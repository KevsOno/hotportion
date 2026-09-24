import asyncio
import json
import os
import hmac
import hashlib
import base64
import logging
import math
import time
import re
import secrets
import contextvars
from datetime import datetime, timedelta, date, timezone
from decimal import Decimal, InvalidOperation
from typing import List, Optional, Dict, Any, Set, Tuple
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo
import aiohttp
import jwt
from jwt import PyJWTError, PyJWKClient
from fastapi import FastAPI, HTTPException, Depends, Query, status, BackgroundTasks, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from supabase import create_client, Client
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# ---------- AI Libraries ----------
try:
    import groq
except ImportError:
    groq = None
try:
    import google.generativeai as genai
except ImportError:
    genai = None
try:
    import openai
except ImportError:
    openai = None

# ---------- Sentry (optional) ----------
try:
    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    _SENTRY_AVAILABLE = True
except ImportError:
    _SENTRY_AVAILABLE = False

load_dotenv()


# ---------- LOCAL TIMEZONE ----------
# Nigeria is UTC+1 year-round (no DST). Peak-hour windows and business hours
# are configured in this timezone, NOT in UTC. Any timestamp entering the
# pricing engine must be normalised to this zone before comparison.
LOCAL_TZ = ZoneInfo("Africa/Lagos")


# ---------- HELPER FUNCTION ----------
def convert_datetime_to_iso(obj):
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    elif isinstance(obj, dict):
        return {k: convert_datetime_to_iso(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [convert_datetime_to_iso(item) for item in obj]
    elif hasattr(obj, "__dict__"):
        return convert_datetime_to_iso(obj.__dict__)
    else:
        return obj


# [FIX] Haversine distance in meters between two lat/lng pairs. Used to
# verify that client-supplied delivery coordinates are plausibly the same
# place as the address the customer actually typed.
def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = (
        math.sin(dphi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    )
    return 2.0 * R * math.asin(math.sqrt(a))


# ---------- CONFIGURATION ----------
class Settings(BaseSettings):
    SUPABASE_URL: str = Field(..., min_length=1)
    SUPABASE_SERVICE_KEY: str = Field(..., min_length=1)
    # JWT secret from Supabase → Project Settings → API → JWT Settings → JWT Secret
    SUPABASE_JWT_SECRET: Optional[str] = None
    SUPABASE_JWT_AUDIENCE: str = "authenticated"
    BREVO_API_KEY: str = Field(..., min_length=1)
    BREVO_SENDER_EMAIL: str = Field(..., min_length=1)
    BREVO_SENDER_NAME: str = "Hot Portion Grill"
    MONNIFY_API_KEY: str = Field(..., min_length=1)
    MONNIFY_SECRET_KEY: str = Field(..., min_length=1)
    MONNIFY_CONTRACT_CODE: str = Field(..., min_length=1)
    MONNIFY_BASE_URL: str = "https://sandbox.monnify.com"
    # [FIX] Monnify redirect/webhook URLs are now config-driven so staging,
    # preview environments, and domain changes don't silently post to prod.
    MONNIFY_REDIRECT_URL: str = "https://hotportion.netlify.app/?status=success"
    MONNIFY_WEBHOOK_URL: str = "https://hotportion.onrender.com/api/v1/webhooks/monnify"
    GROQ_API_KEY: Optional[str] = None
    GEMINI_API_KEY: Optional[str] = None
    OPENAI_API_KEY: Optional[str] = None
    GROQ_MODEL: str = "openai/gpt-oss-120b"
    GEMINI_MODEL: str = "gemini-2.0-flash"
    OPENAI_MODEL: str = "gpt-oss-120"
    AI_PRIMARY: str = "groq"
    AI_FALLBACK: str = "gemini"
    ALLOWED_ORIGINS: List[str] = [
        "https://hotportion.netlify.app",
        "https://hotportion.onrender.com",
        "http://localhost:3000",
        "http://localhost:8000"
    ]
    MAX_DB_THREADS: int = 25
    STATS_CACHE_TTL_SECONDS: int = 10
    DEBUG: bool = False
    MINIMUM_DELIVERY_FEE: int = 500
    MAX_LOYALTY_DISCOUNT_PERCENT: int = 15
    VOLUME_SURCHARGE_THRESHOLDS: Dict[str, int] = {"large": 4, "xlarge": 6, "xxlarge": 10}
    VOLUME_SURCHARGE_AMOUNTS: Dict[str, int] = {"large": 150, "xlarge": 300, "xxlarge": 500}
    WEIGHT_THRESHOLD_KG: float = 5.0
    WEIGHT_SURCHARGE_PER_KG: int = 100
    BULKY_ITEM_SURCHARGE: int = 200
    PEAK_SURCHARGE: int = 200
    AWS_LOCATION_API_KEY: Optional[str] = None
    # Optional: separate key for server-side Geocoding API. Falls back to
    # AWS_LOCATION_API_KEY if not provided.
    AWS_GEOCODING_API_KEY: Optional[str] = None
    AWS_LOCATION_REGION: str = "eu-north-1"
    # Nominatim is a last-resort fallback only (OSM public server has a strict
    # usage policy). Keep this False in production once AWS geocoding is live.
    NOMINATIM_FALLBACK_ENABLED: bool = True
    # Geocode cache (address -> lat/lng). In-memory, per-process.
    GEOCODE_CACHE_TTL_SECONDS: int = 86400       # 24h
    GEOCODE_CACHE_MAX_ENTRIES: int = 5000
    # [FIX] Max allowed distance between client-supplied delivery coordinates
    # and the coordinates the server obtains by geocoding the typed address.
    # Beyond this, the server-side geocode wins. Prevents zone-fee gaming.
    CLIENT_COORD_MAX_DISCREPANCY_M: float = 1000.0
    SENTRY_DSN: Optional[str] = None
    # Where invite and password-reset links should redirect after verification
    INVITE_REDIRECT_URL: str = "https://hotportion.netlify.app/admin"
    DB_QUERY_TIMEOUT_SECONDS: float = 15.0
    RATE_LIMIT_ORDERS_PER_MINUTE: int = 10
    RATE_LIMIT_DELIVERY_PER_MINUTE: int = 30
    RATE_LIMIT_AI_PER_MINUTE: int = 20
    RATE_LIMIT_PLACES_PER_MINUTE: int = 60
    # Order reconciliation (background loop that resolves drift between
    # Monnify and our orders table when the webhook is missed)
    RECONCILIATION_ENABLED: bool = True
    RECONCILIATION_INTERVAL_SECONDS: int = 300       # run every 5 min
    RECONCILIATION_GRACE_SECONDS: int = 300          # skip orders newer than 5 min
    RECONCILIATION_MAX_AGE_HOURS: int = 48           # don't chase ancient orders
    RECONCILIATION_BATCH_SIZE: int = 50
    # [FIX] A pending order whose Monnify transaction is still PENDING after
    # this many minutes is treated as abandoned and cancelled.
    ABANDONED_CHECKOUT_GRACE_MINUTES: int = 30
    # [FIX] A pending order older than this is cancelled unconditionally -
    # backstop for orders where Monnify queries kept failing during the
    # in-window sweep. Monnify auto-expires unpaid checkouts long before this.
    STALE_CHECKOUT_HOURS: int = 6
    # [FEATURE] Offline orders (pickup/dine-in paid at counter) that sit in
    # `awaiting_payment` for this long are flagged as no-shows. We do NOT
    # auto-cancel them - a human should decide - but we log a warning so
    # staff know to chase.
    OFFLINE_NO_SHOW_WARNING_HOURS: int = 8

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"

settings = Settings()

# ---------- LOGGING ----------
_correlation_id_var = contextvars.ContextVar("correlation_id", default="unknown")

class CorrelationFilter(logging.Filter):
    def filter(self, record):
        record.correlation_id = _correlation_id_var.get()
        return True

class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_obj = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "module": record.module,
            "message": record.getMessage(),
        }
        if hasattr(record, "correlation_id"):
            log_obj["correlation_id"] = record.correlation_id
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_obj)

handler = logging.StreamHandler()
handler.setFormatter(JSONFormatter())
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(handler)
logger.addFilter(CorrelationFilter())

uvicorn_logger = logging.getLogger("uvicorn.access")
uvicorn_logger.handlers = [handler]
uvicorn_logger.addFilter(CorrelationFilter())

# ---------- DATABASE ----------
_executor = ThreadPoolExecutor(max_workers=settings.MAX_DB_THREADS)
_supabase_client: Optional[Client] = None

def get_supabase() -> Client:
    global _supabase_client
    if _supabase_client is None:
        _supabase_client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
    return _supabase_client

async def execute_db(query):
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(_executor, query.execute)
    return await asyncio.wait_for(future, timeout=settings.DB_QUERY_TIMEOUT_SECONDS)

# ---------- RATE LIMITER ----------
# NOTE: In-memory. Safe for a single Uvicorn worker. Multi-worker deployments
# must swap this for a shared store (e.g. Redis) - otherwise each worker enforces
# its own quota and effective limits scale with worker count.
_rate_limit_store: Dict[str, List[float]] = {}
_rate_limit_lock = asyncio.Lock()

async def enforce_rate_limit(request: Request, bucket: str, max_per_minute: int) -> None:
    if max_per_minute <= 0:
        return
    client_ip = request.client.host if request.client else "unknown"
    key = f"{bucket}:{client_ip}"
    now = time.time()
    window = 60.0
    async with _rate_limit_lock:
        timestamps = _rate_limit_store.get(key, [])
        cutoff = now - window
        timestamps = [t for t in timestamps if t > cutoff]
        if len(timestamps) >= max_per_minute:
            logger.warning(f"Rate limit hit: bucket={bucket} ip={client_ip} count={len(timestamps)}")
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Please slow down and try again shortly."
            )
        timestamps.append(now)
        _rate_limit_store[key] = timestamps
        if len(_rate_limit_store) > 10000:
            stale_keys = [k for k, v in _rate_limit_store.items() if not v or v[-1] < cutoff]
            for k in stale_keys:
                _rate_limit_store.pop(k, None)

# ============================================================
# AUTHENTICATION & AUTHORIZATION (role-based)
# ============================================================

# Role → set of permissions. "*" means everything.
PERMISSIONS: Dict[str, Set[str]] = {
    "owner": {"*"},
    "manager": {
        "products:read", "products:write",
        "categories:read", "categories:write",
        "banners:read", "banners:write",
        "orders:read", "orders:update_status",
        "delivery_rules:read", "delivery_rules:write",
        "delivery_areas:read", "delivery_areas:write",
        "stats:read",
        "audit:read",
        "staff:read", "staff:write",
    },
    "kitchen": {
        "orders:read", "orders:update_status",
        "products:read",
    },
    "delivery": {
        "orders:read", "orders:update_status",
    },
}

ALLOWED_ROLES = set(PERMISSIONS.keys())


# ─── JWKS client (lazy, cached) ───
# Supabase signs tokens asymmetrically (ES256/RS256). Public keys are published
# at {SUPABASE_URL}/auth/v1/.well-known/jwks.json. PyJWKClient caches them for 1h.
_jwks_client: Optional[PyJWKClient] = None

def _get_jwks_client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        jwks_url = f"{settings.SUPABASE_URL}/auth/v1/.well-known/jwks.json"
        _jwks_client = PyJWKClient(jwks_url, cache_keys=True, lifespan=3600)
        logger.info(f"JWKS client initialized: {jwks_url}")
    return _jwks_client


async def verify_supabase_jwt(token: str) -> Dict[str, Any]:
    """
    Verify a Supabase JWT. Auto-detects the signing algorithm from the token
    header and picks the right verifier:
      - HS256  → shared secret (legacy projects)
      - ES256 / RS256 → JWKS public key (modern Supabase projects)
    """
    # Peek at the header without verifying, to find the algorithm
    try:
        unverified_header = jwt.get_unverified_header(token)
    except PyJWTError as e:
        logger.warning(f"Malformed JWT header: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    alg = (unverified_header.get("alg") or "").upper()
    logger.info(f"JWT verify: alg={alg} kid={unverified_header.get('kid')}")

    try:
        if alg == "HS256":
            # Legacy shared-secret verification
            if not settings.SUPABASE_JWT_SECRET:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Authentication is not configured on this server (SUPABASE_JWT_SECRET missing)",
                )
            payload = jwt.decode(
                token,
                settings.SUPABASE_JWT_SECRET,
                algorithms=["HS256"],
                audience=settings.SUPABASE_JWT_AUDIENCE,
                options={"verify_exp": True},
            )
        elif alg in ("ES256", "RS256"):
            # Modern JWKS-based verification
            signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=["ES256", "RS256"],
                audience=settings.SUPABASE_JWT_AUDIENCE,
                options={"verify_exp": True},
            )
        else:
            logger.warning(f"Unsupported JWT algorithm: {alg!r}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Unsupported token algorithm: {alg}",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return payload

    except HTTPException:
        raise
    except PyJWTError as e:
        logger.warning(f"JWT verification failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def get_current_user(authorization: Optional[str] = Header(None, alias="Authorization")) -> Dict[str, Any]:
    """Extract and verify the Supabase JWT from the Authorization header."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.split(" ", 1)[1].strip()
    payload = await verify_supabase_jwt(token)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token missing 'sub' claim")
    return payload


async def get_current_staff(user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """Look up the staff row for the authenticated user and verify it's active."""
    user_id = user.get("sub")
    try:
        r = await execute_db(
            get_supabase().table("staff").select("*").eq("id", user_id).limit(1)
        )
    except Exception as e:
        logger.error(f"Staff lookup failed for {user_id}: {e}")
        raise HTTPException(status_code=500, detail="Could not verify staff account")

    if not r.data:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account is not registered as staff. Contact an owner."
        )
    staff_row = r.data[0]
    if not staff_row.get("is_active", True):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Your staff account has been deactivated")
    return staff_row


def require_permission(permission: str):
    """Return a FastAPI dependency that enforces a specific permission."""
    async def checker(staff: Dict[str, Any] = Depends(get_current_staff)):
        role = staff.get("role")
        perms = PERMISSIONS.get(role, set())
        if "*" not in perms and permission not in perms:
            logger.warning(f"Permission denied: staff={staff.get('email')} role={role} needed={permission}")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Your role ('{role}') does not permit this action."
            )
        return staff
    return checker


async def audit_log(
    staff: Dict[str, Any],
    action: str,
    resource_type: str,
    resource_id: Optional[Any] = None,
    payload: Optional[Dict[str, Any]] = None,
    request: Optional[Request] = None,
) -> None:
    """Record an admin mutation. Never raises - a failed audit must not break the operation."""
    try:
        await execute_db(get_supabase().table("staff_audit_log").insert({
            "staff_id": staff.get("id"),
            "staff_email": staff.get("email"),
            "action": action,
            "resource_type": resource_type,
            "resource_id": str(resource_id) if resource_id is not None else None,
            "payload": payload,
            "ip_address": request.client.host if request and request.client else None,
            "user_agent": request.headers.get("user-agent") if request else None,
        }))
    except Exception as e:
        logger.warning(f"Audit log write failed: {e}")


# ---------- STAFF PYDANTIC MODELS ----------
class StaffCreate(BaseModel):
    email: str
    full_name: str
    role: str
    # [FIX] Admin-set initial password. Replaces the Supabase invite flow,
    # which was unreliable due to URL-hash fragility. Users can change their
    # password later via the "Forgot password" flow on the login screen.
    password: str = Field(..., min_length=12, max_length=128)

class StaffUpdate(BaseModel):
    full_name: Optional[str] = None
    role: Optional[str] = None

class StaffMember(BaseModel):
    id: str
    email: str
    full_name: str
    role: str
    is_active: bool
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


# ---------- ORDER STATE MACHINE ----------
# [FEATURE] `awaiting_payment` is a new status for offline orders (pickup or
# dine-in that will be paid at the counter on arrival). It is distinct from
# `pending`, which continues to mean "customer is at Monnify checkout".
# Keeping them separate is essential: the reconciliation loop auto-cancels
# stale `pending` rows, but must never touch `awaiting_payment` - those are
# legitimately waiting for a human.
_ALLOWED_ORDER_TRANSITIONS: Dict[str, Set[str]] = {
    "pending":           {"paid", "confirmed", "cancelled"},
    "awaiting_payment":  {"paid", "confirmed", "completed", "cancelled"},
    "paid":              {"confirmed", "completed", "cancelled"},
    "confirmed":         {"completed", "cancelled"},
    "cancelled":         set(),
    "completed":         set(),
}

def validate_order_transition(current: Optional[str], target: str) -> None:
    if not current or not target:
        return
    current_lc = current.lower()
    target_lc = target.lower()
    if current_lc == target_lc:
        return
    allowed = _ALLOWED_ORDER_TRANSITIONS.get(current_lc)
    if allowed is None:
        logger.error(
            f"Unknown current order status '{current}'. Refusing transition to '{target}'."
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Unknown current order status: {current}",
        )
    if target_lc not in allowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status transition: {current} → {target}"
        )


def payment_amount_matches(expected_amount: Any, paid_amount: Any) -> bool:
    """Return True only when the provider paid at least the order total."""
    try:
        expected = Decimal(str(expected_amount))
        paid = Decimal(str(paid_amount))
    except (InvalidOperation, TypeError, ValueError):
        return False
    if expected < 0 or paid < 0:
        return False
    return paid >= expected

# ---------- PYDANTIC MODELS ----------
class ProductBase(BaseModel):
    name: str
    description: Optional[str] = None
    price: int
    stock: int = 0
    tag: str
    emoji: str = "🍽️"
    image: Optional[str] = None
    tagColor: str = "primary"
    is_main_item: bool = True
    weight_kg: float = 0.5
    is_bulky: bool = False

class ProductCreate(ProductBase): pass
class ProductUpdate(ProductBase): pass
class Product(ProductBase):
    id: int

class CategoryBase(BaseModel):
    name: str
class Category(CategoryBase):
    id: int

class OrderItem(BaseModel):
    name: str
    qty: int
    price: int
    product_id: int

class OrderCreate(BaseModel):
    payment_reference: Optional[str] = None
    customer_name: str
    customer_email: str
    customer_phone: str
    total: Optional[int] = None
    status: Optional[str] = "pending"
    delivery_method: Optional[str] = "pickup"
    # [FEATURE] Payment method - "online" (through Monnify) or "offline"
    # (pay at the counter on arrival). Only valid for pickup and dine-in.
    # Delivery orders must be "online". Default "online" preserves the
    # previous behaviour for clients that don't send this field.
    payment_method: Optional[str] = "online"
    delivery_address: Optional[str] = None
    preferred_time: Optional[str] = None
    order_notes: Optional[str] = None
    items: List[OrderItem]
    monnify_transaction_ref: Optional[str] = None
    delivery_fee: Optional[int] = 0
    delivery_breakdown: Optional[Dict[str, Any]] = None
    # Client-resolved coordinates (from AWS Places autocomplete). When present,
    # the server VERIFIES them against the typed address before trusting them.
    lat: Optional[float] = None
    lng: Optional[float] = None
    # [FIX] Idempotency key. Clients should generate one UUID per checkout
    # attempt and send it on every retry of that attempt. The server stores it
    # on the order row; a repeat POST with the same key returns the existing
    # order instead of creating a duplicate + a second Monnify transaction.
    idempotency_key: Optional[str] = None

class OrderStatusUpdate(BaseModel):
    status: str
    reason: Optional[str] = None

class BannerBase(BaseModel):
    title: str
    subtitle: Optional[str] = None
    description: Optional[str] = None
    image_url: Optional[str] = None
    cta_text: Optional[str] = None
    cta_link: Optional[str] = None
    cta_type: str = "button"
    product_id: Optional[int] = None
    badge_text: Optional[str] = None
    badge_color: str = "#FF5722"
    background_color: str = "#fff3ed"
    text_color: str = "#1e1e1e"
    position: int = 0
    is_active: bool = True
    is_hero: bool = False
    is_featured: bool = False
    display_order: int = 0
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    discount_type: Optional[str] = None
    discount_value: Optional[int] = None
    meta_data: Optional[Dict[str, Any]] = None

class BannerCreate(BannerBase):
    categories: Optional[List[int]] = []
    products: Optional[List[int]] = []

class BannerUpdate(BannerBase):
    categories: Optional[List[int]] = None
    products: Optional[List[int]] = None

class Banner(BannerBase):
    id: int
    created_at: datetime
    updated_at: datetime
    categories: Optional[List[int]] = []
    products: Optional[List[int]] = []

class BannerResponse(BaseModel):
    banners: List[Banner]
    total: int
    active_count: int
    hero_count: int
    featured_count: int

class AIChatRequest(BaseModel):
    message: str
    context: Optional[Dict[str, Any]] = None

class AIChatResponse(BaseModel):
    response: str
    provider: str
    model: str

class DeliveryAreaBase(BaseModel):
    name: str
    fee: int

class DeliveryAreaCreate(DeliveryAreaBase):
    polygon: Dict[str, Any]

class DeliveryAreaUpdate(DeliveryAreaBase):
    polygon: Dict[str, Any]

class DeliveryArea(DeliveryAreaBase):
    id: int
    polygon: Dict[str, Any]
    created_at: datetime
    updated_at: datetime

class DeliveryFeeRule(BaseModel):
    id: Optional[int] = None
    min_order_value: int
    max_order_value: Optional[int] = None
    fee_multiplier: float = 1.0
    fee_discount: int = 0
    free_delivery: bool = False
    description: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

class DeliveryFeeRuleCreate(DeliveryFeeRule):
    pass

class DeliveryFeeRuleUpdate(DeliveryFeeRule):
    pass

class DeliveryPeakSetting(BaseModel):
    id: Optional[int] = None
    day_of_week: Optional[int] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    surcharge_amount: int = 200
    is_active: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

class DeliveryPeakSettingCreate(DeliveryPeakSetting):
    pass

class DeliveryPeakSettingUpdate(DeliveryPeakSetting):
    pass

class DeliveryLoyaltySetting(BaseModel):
    id: Optional[int] = None
    min_orders: int = 5
    discount_percentage: int = 20
    is_active: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

class DeliveryLoyaltySettingCreate(DeliveryLoyaltySetting):
    pass

class DeliveryLoyaltySettingUpdate(DeliveryLoyaltySetting):
    pass

class DeliveryItemSurchargeRule(BaseModel):
    id: Optional[int] = None
    min_main_items: Optional[int] = None
    max_main_items: Optional[int] = None
    surcharge_amount: int = 0
    weight_threshold_kg: Optional[float] = None
    surcharge_per_kg: Optional[int] = None
    description: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

class DeliveryItemSurchargeRuleCreate(DeliveryItemSurchargeRule):
    pass

class DeliveryItemSurchargeRuleUpdate(DeliveryItemSurchargeRule):
    pass

class DeliveryFeeRequest(BaseModel):
    address: str
    items: List[OrderItem]
    order_total: int
    order_time: Optional[datetime] = None
    customer_email: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None

class DeliveryFeeResponse(BaseModel):
    covered: bool
    base_fee: Optional[int] = None
    area_name: Optional[str] = None
    volume_surcharge: int = 0
    value_discount: int = 0
    item_surcharge: int = 0
    peak_surcharge: int = 0
    loyalty_discount: int = 0
    total_fee: int = 0
    breakdown: Optional[Dict[str, Any]] = None
    message: Optional[str] = None

# ---------- AI SERVICE ----------
class AIService:
    def __init__(self):
        self.groq_client = None
        self.genai_client = None
        self.openai_client = None
        self.supabase_client: Optional[Client] = None
        self._context_cache: Optional[str] = None
        self._cache_timestamp: float = 0
        self._cache_ttl: int = 3600
        self._init_clients()
        self.banned_words = {
            "kill", "murder", "hate", "racist", "sex", "porn", "assault", "terror", "bomb",
            "shoot", "stab", "rape", "slave", "abuse", "harass"
        }
        self.base_system_prompt = (
            "You are an AI assistant for 'Hot Portion Grill', a Nigerian restaurant. "
            "Help customers with menu, prices, orders, special offers, and food queries. "
            "Answer strictly based on the provided PRODUCTS and KNOWLEDGE BASE below. "
            "If an item or answer is not in the provided information, state politely that it is unavailable. "
            "Do not answer questions completely unrelated to food, restaurants, or ordering. "
            "Keep responses concise, friendly, and professional. "
            "When a user asks for the 'menu', 'what do you have', or 'list all items', "
            "respond with a clear list of all available products from the PRODUCTS section, "
            "including name and price. If the list is long, provide a summary and offer to give more details. "
            "When providing information, format your response for readability:\n"
            "- Use bullet points (hyphens) for lists.\n"
            "- Put **item names** or **headings** in bold using asterisks (e.g., **Jollof Rice**).\n"
            "- Display prices as ₦X,XXX.\n"
            "- Use emojis sparingly to add visual cues (e.g., 🍚 for rice, 📍 for location, ⏰ for hours).\n"
            "- For menus, group items by category (e.g., Rice Dishes, Swallows) if possible.\n"
            "- For services, use a clear structure with short headings (e.g., **Delivery** – ...).\n"
            "- Keep lines short and use blank lines between sections for readability."
        )

    def _init_clients(self):
        supabase_url = getattr(settings, 'SUPABASE_URL', None)
        supabase_key = getattr(settings, 'SUPABASE_SERVICE_KEY', None)
        if supabase_url and supabase_key:
            try:
                self.supabase_client = create_client(supabase_url, supabase_key)
                logger.info("Supabase client initialized successfully")
            except Exception as e:
                logger.error(f"Failed to initialize Supabase client: {e}")
        else:
            logger.warning("Supabase credentials not configured")

        if getattr(settings, 'GROQ_API_KEY', None) and groq is not None:
            self.groq_client = groq.Groq(api_key=settings.GROQ_API_KEY)
            logger.info("Groq client initialized")
        else:
            logger.warning("Groq client not available")

        if getattr(settings, 'GEMINI_API_KEY', None) and genai is not None:
            genai.configure(api_key=settings.GEMINI_API_KEY)
            self.genai_client = genai.GenerativeModel(settings.GEMINI_MODEL)
            logger.info("Gemini client initialized")
        else:
            logger.warning("Gemini client not available")

        if getattr(settings, 'OPENAI_API_KEY', None) and openai is not None:
            self.openai_client = openai.OpenAI(api_key=settings.OPENAI_API_KEY)
            logger.info("OpenAI client initialized")
        else:
            logger.warning("OpenAI client not available")

    def _get_context(self) -> str:
        current_time = time.time()
        if self._context_cache and (current_time - self._cache_timestamp < self._cache_ttl):
            return self._context_cache
        if not self.supabase_client:
            logger.warning("Supabase client unavailable, skipping database context fetch.")
            return ""
        try:
            products_res = self.supabase_client.table("products").select("*").execute()
            products = products_res.data if products_res.data else []
            knowledge_res = self.supabase_client.table("knowledge").select("*").execute()
            knowledge = knowledge_res.data if knowledge_res.data else []
            context = "\n\n=== PRODUCTS / MENU ===\n"
            for p in products:
                name = p.get('name', 'Item')
                price = p.get('price', 'N/A')
                desc = p.get('description', 'N/A')
                status = p.get('status', 'available')
                context += f"- {name}: {price} | Desc: {desc} | Status: {status}\n"
            context += "\n=== KNOWLEDGE BASE & STORE INFO ===\n"
            for k in knowledge:
                topic = k.get('topic', 'Information')
                content = k.get('content', '')
                context += f"- {topic}: {content}\n"
            self._context_cache = context
            self._cache_timestamp = current_time
            logger.info("Supabase menu & knowledge base context refreshed")
            return context
        except Exception as e:
            logger.error(f"Error fetching Supabase context: {e}")
            return self._context_cache or ""

    def _build_system_prompt(self) -> str:
        context = self._get_context()
        return f"{self.base_system_prompt}{context}"

    def _normalize(self, text: str) -> str:
        text = text.lower()
        replacements = {'3': 'e', '4': 'a', '0': 'o', '@': 'a', '$': 's', '5': 's'}
        for old, new in replacements.items():
            text = text.replace(old, new)
        return text

    def _guard_input(self, text: str) -> bool:
        patterns = [
            r"ignore (?:all )?previous instructions",
            r"forget (?:all )?previous (?:instructions|context)",
            r"you are (?:now )?a (?:new )?ai",
            r"system prompt",
            r"override (?:the )?system",
        ]
        for p in patterns:
            if re.search(p, text, re.IGNORECASE):
                logger.warning(f"Prompt injection attempt blocked: {text[:50]}...")
                return False
        norm_text = self._normalize(text)
        words = set(re.findall(r'\b\w+\b', norm_text))
        if words.intersection(self.banned_words):
            logger.warning(f"Banned word detected in input: {text[:50]}...")
            return False
        return True

    def _guard_output(self, text: str) -> bool:
        norm_text = self._normalize(text)
        words = set(re.findall(r'\b\w+\b', norm_text))
        if words.intersection(self.banned_words):
            logger.warning(f"Banned word detected in output: {text[:50]}...")
            return False
        if len(text) < 2 or len(text) > 2000:
            return False
        return True

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=3))
    async def query_groq(self, msg: str, system_prompt: str, history: List[Dict[str, str]]) -> Optional[str]:
        if not self.groq_client:
            raise ValueError("Groq unavailable")
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history)
        messages.append({"role": "user", "content": msg})
        resp = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: self.groq_client.chat.completions.create(
                model=settings.GROQ_MODEL, messages=messages, temperature=0.3,
                max_tokens=700, timeout=10.0
            )
        )
        return resp.choices[0].message.content

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=3))
    async def query_gemini(self, msg: str, system_prompt: str, history: List[Dict[str, str]]) -> Optional[str]:
        if not self.genai_client:
            raise ValueError("Gemini unavailable")
        formatted_history = ""
        for h in history:
            role = "User" if h.get("role") == "user" else "Assistant"
            formatted_history += f"\n{role}: {h.get('content', '')}"
        full = f"{system_prompt}\n{formatted_history}\nUser: {msg}\nAssistant:"
        response = await asyncio.get_event_loop().run_in_executor(
            None, self.genai_client.generate_content, full
        )
        return response.text

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=3))
    async def query_openai(self, msg: str, system_prompt: str, history: List[Dict[str, str]]) -> Optional[str]:
        if not self.openai_client:
            raise ValueError("OpenAI unavailable")
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history)
        messages.append({"role": "user", "content": msg})
        resp = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: self.openai_client.chat.completions.create(
                model=settings.OPENAI_MODEL, messages=messages, temperature=0.3,
                max_tokens=700, timeout=10.0
            )
        )
        return resp.choices[0].message.content

    async def chat(self, msg: str, history: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
        if not self._guard_input(msg):
            return {"response": "I cannot process that request.", "provider": "guardrail", "model": "blocked"}
        conversation_history = history[-6:] if history else []
        system_prompt = self._build_system_prompt()
        available = []
        if self.groq_client:
            available.append(("groq", self.query_groq, settings.GROQ_MODEL))
        if self.genai_client:
            available.append(("gemini", self.query_gemini, settings.GEMINI_MODEL))
        if self.openai_client:
            available.append(("openai", self.query_openai, settings.OPENAI_MODEL))
        if not available:
            return {"response": "No AI provider available.", "provider": "error", "model": "none"}
        ordered = []
        for p in available:
            if p[0] == settings.AI_PRIMARY:
                ordered.append(p)
                break
        if settings.AI_FALLBACK != settings.AI_PRIMARY:
            for p in available:
                if p[0] == settings.AI_FALLBACK and p not in ordered:
                    ordered.append(p)
                    break
        for p in available:
            if p not in ordered:
                ordered.append(p)
        for name, func, model in ordered:
            try:
                content = await func(msg, system_prompt, conversation_history)
                if content and self._guard_output(content):
                    return {"response": content, "provider": name, "model": model}
            except Exception as e:
                logger.warning(f"Provider {name} failed: {e}")
                continue
        return {"response": "I'm currently unable to respond. Please try again.", "provider": "error", "model": "none"}

# ---------- BREVO ----------
class BrevoIntegration:
    def __init__(self):
        self.api_key = settings.BREVO_API_KEY
        self.base_url = "https://api.brevo.com/v3"
        self.sender = {"email": settings.BREVO_SENDER_EMAIL, "name": settings.BREVO_SENDER_NAME}
        self._session = None
        self.healthy = False

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        return self._session

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=5),
           retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)))
    async def initialize(self):
        headers = {"api-key": self.api_key, "Content-Type": "application/json"}
        sess = await self._get_session()
        async with sess.get(f"{self.base_url}/account", headers=headers) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Brevo failed: {resp.status}")
        self.healthy = True
        logger.info("Brevo ready")

    async def send_order_confirmation(self, data: Dict) -> bool:
        if not self.healthy:
            logger.warning("Brevo unhealthy, skipping email")
            return False
        try:
            items_html = "".join(
                f"<tr><td>{i['name']}</td><td>{i['qty']}</td><td>₦{i['price']*i['qty']:,}</td></tr>"
                for i in data.get("items", [])
            )
            # [FEATURE] Offline orders get a "pay at counter" note. Online orders
            # keep the original wording (no payment note - the customer just paid).
            payment_html = ""
            if data.get("payment_method") == "offline":
                payment_html = (
                    '<p><strong>Payment:</strong> 💵 Please pay at the counter on arrival. '
                    'Bring this order reference.</p>'
                )
            html = f"""
            <html><body>
            <h2>Order #{data['payment_reference']}</h2>
            <p><strong>Customer:</strong> {data['customer_name']}</p>
            <p><strong>Phone:</strong> {data['customer_phone']}</p>
            <p><strong>Delivery:</strong> {data.get('delivery_method', 'Pickup')}</p>
            <p><strong>Delivery Fee:</strong> ₦{data.get('delivery_fee', 0):,}</p>
            {payment_html}
            <table border=1><tr><th>Item</th><th>Qty</th><th>Price</th></tr>
            {items_html}
            <tr><td colspan=2><b>Total</b></td><td><b>₦{data['total']:,}</b></td></tr>
            </table>
            <p>📍 Ojo Road Aiyenero Junction, Ajegunle Apapa</p>
            </body></html>
            """
            payload = {
                "sender": self.sender,
                "to": [{"email": data["customer_email"], "name": data["customer_name"]}],
                "subject": f"Order #{data['payment_reference']}",
                "htmlContent": html,
            }
            headers = {"api-key": self.api_key, "Content-Type": "application/json"}
            sess = await self._get_session()
            async with sess.post(f"{self.base_url}/smtp/email", json=payload, headers=headers) as resp:
                if resp.status == 201:
                    logger.info(f"Email sent to {data['customer_email']}")
                    return True
                logger.error(f"Email failed: {await resp.text()}")
                return False
        except Exception as e:
            logger.error(f"Email error: {e}")
            return False

# ---------- MONNIFY ----------
class MonnifyIntegration:
    def __init__(self):
        self.api_key = settings.MONNIFY_API_KEY
        self.secret_key = settings.MONNIFY_SECRET_KEY
        self.contract_code = settings.MONNIFY_CONTRACT_CODE
        self.base_url = settings.MONNIFY_BASE_URL
        self._token = None
        self._token_expiry = None
        self._session = None
        self.healthy = False

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self._session

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=5))
    async def initialize(self):
        await self._get_access_token()
        self.healthy = True
        logger.info("Monnify ready")

    async def _get_access_token(self) -> str:
        if self._token and self._token_expiry and datetime.now() < self._token_expiry:
            return self._token
        auth = base64.b64encode(f"{self.api_key}:{self.secret_key}".encode()).decode()
        headers = {"Authorization": f"Basic {auth}", "Content-Type": "application/json"}
        sess = await self._get_session()
        async with sess.post(f"{self.base_url}/api/v1/auth/login", headers=headers) as resp:
            data = await resp.json()
            if resp.status == 200:
                self._token = data["responseBody"]["accessToken"]
                self._token_expiry = datetime.now() + timedelta(hours=1)
                return self._token
            raise RuntimeError(f"Monnify auth failed: {data}")

    async def initialize_transaction(
        self, amount: int, customer_name: str, customer_email: str, customer_phone: str,
        payment_reference: str, payment_description: str = "Hot Portion Grill Order"
    ) -> Dict[str, Any]:
        if not self.healthy:
            try:
                await self.initialize()
            except Exception as e:
                return {"success": False, "error": f"Monnify not healthy: {e}"}
        token = await self._get_access_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        payload = {
            "amount": amount,
            "customerName": customer_name,
            "customerEmail": customer_email,
            "customerPhone": customer_phone,
            "paymentReference": payment_reference,
            "paymentDescription": payment_description,
            "contractCode": self.contract_code,
            "currencyCode": "NGN",
            "paymentMethods": ["CARD", "ACCOUNT_TRANSFER"],
            # [FIX] URLs are config-driven (staging/preview no longer route to prod).
            "redirectUrl": settings.MONNIFY_REDIRECT_URL,
            "webhookUrl": settings.MONNIFY_WEBHOOK_URL,
        }
        sess = await self._get_session()
        try:
            async with sess.post(
                f"{self.base_url}/api/v1/merchant/transactions/init-transaction",
                json=payload, headers=headers
            ) as resp:
                data = await resp.json()
                if resp.status == 200 and data.get("requestSuccessful"):
                    body = data.get("responseBody", {})
                    return {
                        "success": True,
                        "transaction_reference": body.get("transactionReference"),
                        "checkout_url": body.get("checkoutUrl"),
                    }
                else:
                    error_msg = data.get("responseMessage", "Unknown Monnify error")
                    logger.error(f"Monnify init failed: {data}")
                    return {"success": False, "error": error_msg}
        except Exception as e:
            logger.exception("Monnify init exception")
            return {"success": False, "error": str(e)}

    async def handle_webhook(self, raw_body: bytes, signature: Optional[str]) -> Dict:
        if not signature:
            return {"valid": False, "error": "Missing signature"}
        computed = hmac.new(self.secret_key.encode(), raw_body, hashlib.sha512).hexdigest()
        if not hmac.compare_digest(computed, signature):
            return {"valid": False, "error": "Invalid signature"}
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return {"valid": False, "error": f"Invalid JSON body: {e}"}
        return {"valid": True, "event": payload.get("eventType"), "payload": payload}

    async def query_transaction(self, transaction_reference: str) -> Dict[str, Any]:
        """
        Fetch the current status of a Monnify transaction by transactionReference.
        Used by the reconciliation loop to detect PAID/FAILED transactions whose
        webhook never arrived.

        Returns:
          {"success": True, "body": {...}}  on 200
          {"success": False, "error": "..."} otherwise
        """
        if not transaction_reference:
            return {"success": False, "error": "Missing transaction reference"}
        if not self.healthy:
            try:
                await self.initialize()
            except Exception as e:
                return {"success": False, "error": f"Monnify not healthy: {e}"}
        try:
            token = await self._get_access_token()
            headers = {"Authorization": f"Bearer {token}"}
            url = f"{self.base_url}/api/v2/transactions/{transaction_reference}"
            sess = await self._get_session()
            async with sess.get(url, headers=headers) as resp:
                try:
                    data = await resp.json()
                except Exception:
                    text = await resp.text()
                    return {"success": False, "error": f"Non-JSON response ({resp.status}): {text[:120]}"}
                if resp.status == 200 and data.get("requestSuccessful"):
                    return {"success": True, "body": data.get("responseBody", {})}
                return {
                    "success": False,
                    "error": data.get("responseMessage", f"HTTP {resp.status}"),
                    "status_code": resp.status,
                }
        except Exception as e:
            logger.warning(f"Monnify query_transaction({transaction_reference}) failed: {e}")
            return {"success": False, "error": str(e)}

# ---------- AWS LOCATION (server-side geocoding) ----------
# Wraps the AWS Location Service v2 Geocoding / Reverse-Geocoding APIs. The
# same API key that powers the frontend Places autocomplete can be used here,
# but a separate AWS_GEOCODING_API_KEY is honoured if configured (AWS allows
# scoping keys per API - a good practice).
class AWSService:
    def __init__(self):
        self.region = settings.AWS_LOCATION_REGION
        self.places_api_key = settings.AWS_LOCATION_API_KEY
        self.geocoding_api_key = settings.AWS_GEOCODING_API_KEY or settings.AWS_LOCATION_API_KEY
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        return self._session

    @property
    def geocoding_configured(self) -> bool:
        return bool(self.geocoding_api_key)

    async def geocode(self, address: str) -> Optional[Tuple[float, float]]:
        """
        Forward-geocode a free-form address via AWS Location Service v2
        (`/geocode`). Returns (lat, lng) or None if not found / not configured.

        AWS response shape (v2):
          {
            "ResultItems": [
              {"PlaceId": "...", "PlaceType": "...", "Title": "...",
               "Position": [lng, lat], "Address": {...}, ...}
            ]
          }
        """
        if not self.geocoding_configured:
            logger.warning("AWS geocoding not configured (missing AWS_LOCATION_API_KEY / AWS_GEOCODING_API_KEY)")
            return None
        url = f"https://places.geo.{self.region}.amazonaws.com/v2/geocode"
        params = {"key": self.geocoding_api_key}
        body = {"QueryText": address, "MaxResults": 1}
        try:
            sess = await self._get_session()
            async with sess.post(url, params=params, json=body) as resp:
                text = await resp.text()
                if resp.status != 200:
                    logger.warning(f"AWS geocode -> {resp.status}: {text[:200]}")
                    return None
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    logger.warning(f"AWS geocode returned non-JSON: {text[:200]}")
                    return None
                items = data.get("ResultItems") or []
                if not items:
                    return None
                pos = items[0].get("Position")  # [lng, lat]
                if not pos or len(pos) < 2:
                    return None
                lng, lat = float(pos[0]), float(pos[1])
                return (lat, lng)
        except asyncio.TimeoutError:
            logger.warning(f"AWS geocode timed out for address: {address[:80]}")
            return None
        except aiohttp.ClientError as e:
            logger.warning(f"AWS geocode client error: {e}")
            return None
        except Exception as e:
            logger.warning(f"AWS geocode unexpected error: {e}")
            return None

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

# ---------- GEOCODING (AWS-first, cached, Nominatim last-resort) ----------
# Single source of truth for turning an address string into (lat, lng). Order:
#   1. In-memory cache (normalized address key).
#   2. AWS Location Service geocode.
#   3. Nominatim (only if NOMINATIM_FALLBACK_ENABLED=true).
# Every successful result is cached so preview + checkout agree and we don't
# hammer upstream providers.
_geocode_cache: Dict[str, Dict[str, Any]] = {}
_geocode_cache_lock = asyncio.Lock()

def _normalize_address_key(address: str) -> str:
    """Lowercase, collapse whitespace, strip trailing punctuation."""
    if not address:
        return ""
    s = address.strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[.,;]+$", "", s)
    return s

async def _geocode_cache_get(key: str) -> Optional[Tuple[float, float]]:
    if not key:
        return None
    async with _geocode_cache_lock:
        entry = _geocode_cache.get(key)
        if not entry:
            return None
        if entry["expires_at"] < time.time():
            _geocode_cache.pop(key, None)
            return None
        return entry["coords"]

async def _geocode_cache_put(key: str, coords: Tuple[float, float]) -> None:
    if not key or not coords:
        return
    async with _geocode_cache_lock:
        _geocode_cache[key] = {
            "coords": coords,
            "expires_at": time.time() + settings.GEOCODE_CACHE_TTL_SECONDS,
        }
        # Evict expired entries if we're over the cap
        if len(_geocode_cache) > settings.GEOCODE_CACHE_MAX_ENTRIES:
            now = time.time()
            stale = [k for k, v in _geocode_cache.items() if v["expires_at"] < now]
            for k in stale:
                _geocode_cache.pop(k, None)
            # Still over cap? Drop oldest by expiry.
            if len(_geocode_cache) > settings.GEOCODE_CACHE_MAX_ENTRIES:
                ordered = sorted(_geocode_cache.items(), key=lambda kv: kv[1]["expires_at"])
                overflow = len(_geocode_cache) - settings.GEOCODE_CACHE_MAX_ENTRIES
                for k, _ in ordered[:overflow]:
                    _geocode_cache.pop(k, None)

async def _nominatim_geocode(address: str) -> Optional[Tuple[float, float]]:
    """Last-resort fallback. Only used when AWS geocoding fails and fallback is enabled."""
    if not settings.NOMINATIM_FALLBACK_ENABLED:
        return None
    try:
        geocode_url = "https://nominatim.openstreetmap.org/search"
        params = {"q": address, "format": "json", "limit": 1}
        headers = {"User-Agent": "HotPortionGrill/1.0"}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            async with session.get(geocode_url, params=params, headers=headers) as resp:
                if resp.status != 200:
                    logger.warning(f"Nominatim fallback -> {resp.status}")
                    return None
                data = await resp.json()
                if not data:
                    return None
                return (float(data[0].get("lat", 0)), float(data[0].get("lon", 0)))
    except Exception as e:
        logger.warning(f"Nominatim fallback failed: {e}")
        return None

async def geocode_address(address: str) -> Optional[Tuple[float, float]]:
    """
    Resolve a free-form address string to (lat, lng).

    Strategy (in order):
      1. Cache hit (normalized address key).
      2. AWS Location Service geocode.
      3. Nominatim (only if enabled) - logged at WARNING so operators see when
         the fallback is being exercised.

    Returns None if no provider could resolve the address.
    """
    if not address or not address.strip():
        return None
    key = _normalize_address_key(address)

    cached = await _geocode_cache_get(key)
    if cached:
        return cached

    # 1. AWS
    aws = get_aws()
    coords = await aws.geocode(address)
    if coords:
        await _geocode_cache_put(key, coords)
        return coords

    # 2. Nominatim fallback
    if settings.NOMINATIM_FALLBACK_ENABLED:
        logger.warning(
            f"AWS geocode failed for '{address[:80]}' - falling back to Nominatim "
            f"(this path violates OSM usage policy if used frequently; investigate "
            f"AWS geocoding failures)."
        )
        coords = await _nominatim_geocode(address)
        if coords:
            await _geocode_cache_put(key, coords)
            return coords
    else:
        logger.warning(f"AWS geocode failed for '{address[:80]}' and Nominatim fallback is disabled")

    return None


# [FIX] Verify client-supplied coordinates against the typed address.
# Policy:
#   * Server always geocodes the address (unless the caller already has cached coords).
#   * If client coords agree with the server geocode within CLIENT_COORD_MAX_DISCREPANCY_M,
#     trust the client (their coords come from a picked autocomplete result, usually
#     more precise than a string geocode).
#   * If they disagree by more than the threshold, use the server coords and log
#     at WARNING - this is the "customer is spoofing coordinates to land in a cheaper
#     delivery zone" case.
#   * If only one side resolves, use whatever we have.
#   * If neither resolves, return None - callers MUST fail closed.
async def _resolve_delivery_coords(
    address: Optional[str],
    client_lat: Optional[float],
    client_lng: Optional[float],
    customer_email: Optional[str] = None,
) -> Optional[Tuple[float, float]]:
    if not address or not address.strip():
        return None

    client_coords: Optional[Tuple[float, float]] = None
    if client_lat is not None and client_lng is not None:
        try:
            client_coords = (float(client_lat), float(client_lng))
        except (TypeError, ValueError):
            client_coords = None

    server_coords = await geocode_address(address)

    if server_coords and client_coords:
        dist_m = _haversine_m(server_coords[0], server_coords[1], client_coords[0], client_coords[1])
        if dist_m <= settings.CLIENT_COORD_MAX_DISCREPANCY_M:
            return client_coords
        logger.warning(
            f"Delivery coords mismatch for '{address[:80]}' "
            f"(customer={customer_email or 'unknown'}): "
            f"client=({client_coords[0]:.4f},{client_coords[1]:.4f}) "
            f"server=({server_coords[0]:.4f},{server_coords[1]:.4f}) "
            f"distance={dist_m:.0f}m > {settings.CLIENT_COORD_MAX_DISCREPANCY_M:.0f}m - "
            f"using server coords."
        )
        return server_coords

    return server_coords or client_coords


# ---------- SINGLETONS ----------
_brevo = None
_monnify = None
_ai_service = None
_aws = None

def get_brevo():
    global _brevo
    if _brevo is None:
        _brevo = BrevoIntegration()
    return _brevo

def get_monnify():
    global _monnify
    if _monnify is None:
        _monnify = MonnifyIntegration()
    return _monnify

def get_ai_service():
    global _ai_service
    if _ai_service is None:
        _ai_service = AIService()
    return _ai_service

def get_aws() -> AWSService:
    global _aws
    if _aws is None:
        _aws = AWSService()
    return _aws

# ---------- CACHE ----------
_stats_cache = {"data": None, "timestamp": 0}

async def get_cached_stats():
    now = time.time()
    if now - _stats_cache["timestamp"] < settings.STATS_CACHE_TTL_SECONDS and _stats_cache["data"] is not None:
        return _stats_cache["data"]
    db = get_supabase()
    products, categories, orders, revenue = await asyncio.gather(
        execute_db(db.table("products").select("id", count="exact")),
        execute_db(db.table("categories").select("id", count="exact")),
        # [FIX] Exclude checkout-in-progress rows - they are not orders.
        # NOTE: `awaiting_payment` (offline orders) IS counted - those are real
        # orders that staff are preparing, they just haven't been paid yet.
        execute_db(db.table("orders").select("id", count="exact").neq("status", "pending")),
        # [FIX] Revenue only counts money that actually landed. An abandoned
        # checkout never paid, so its total should not appear in revenue.
        # Offline orders in `awaiting_payment` also don't count - money hasn't
        # changed hands yet. They join revenue when staff marks them paid.
        execute_db(
            db.table("orders")
            .select("total")
            .in_("status", ["paid", "confirmed", "completed"])
        )
    )
    result = {
        "totalProducts": products.count,
        "totalCategories": categories.count,
        "totalOrders": orders.count,
        "totalRevenue": sum(o["total"] for o in revenue.data)
    }
    _stats_cache["data"] = result
    _stats_cache["timestamp"] = now
    return result

def invalidate_stats_cache():
    _stats_cache["timestamp"] = 0

# =============================================
# INTELLIGENT DELIVERY FEE ENGINE
# =============================================
_rules_cache = {"data": None, "timestamp": 0}
_peak_cache = {"data": None, "timestamp": 0}
_loyalty_cache = {"data": None, "timestamp": 0}
_item_surcharge_cache = {"data": None, "timestamp": 0}

async def get_delivery_fee_rules() -> List[Dict]:
    now = time.time()
    if now - _rules_cache["timestamp"] < 60 and _rules_cache["data"] is not None:
        return _rules_cache["data"]
    db = get_supabase()
    result = await execute_db(db.table("delivery_fee_rules").select("*").order("min_order_value"))
    rules = result.data or []
    _rules_cache["data"] = rules
    _rules_cache["timestamp"] = now
    return rules

async def get_peak_settings() -> List[Dict]:
    now = time.time()
    if now - _peak_cache["timestamp"] < 60 and _peak_cache["data"] is not None:
        return _peak_cache["data"]
    db = get_supabase()
    result = await execute_db(db.table("delivery_peak_settings").select("*").eq("is_active", True))
    settings_list = result.data or []
    _peak_cache["data"] = settings_list
    _peak_cache["timestamp"] = now
    return settings_list

async def get_loyalty_setting() -> Optional[Dict]:
    now = time.time()
    if now - _loyalty_cache["timestamp"] < 60 and _loyalty_cache["data"] is not None:
        return _loyalty_cache["data"]
    db = get_supabase()
    result = await execute_db(
        db.table("delivery_loyalty_settings").select("*").eq("is_active", True).limit(1)
    )
    setting = result.data[0] if result.data else None
    _loyalty_cache["data"] = setting
    _loyalty_cache["timestamp"] = now
    return setting

async def get_item_surcharge_rules() -> List[Dict]:
    now = time.time()
    if now - _item_surcharge_cache["timestamp"] < 60 and _item_surcharge_cache["data"] is not None:
        return _item_surcharge_cache["data"]
    db = get_supabase()
    result = await execute_db(db.table("delivery_item_surcharge_rules").select("*").order("min_main_items"))
    rules = result.data or []
    _item_surcharge_cache["data"] = rules
    _item_surcharge_cache["timestamp"] = now
    return rules


def is_peak_hour(order_time: Optional[datetime], peak_settings: List[Dict]) -> bool:
    """
    Check if `order_time` falls inside any configured peak window.

    Peak settings are configured in WAT (Africa/Lagos, UTC+1, no DST). Any
    incoming timestamp is normalised to WAT before comparison, so a naive UTC
    timestamp from `datetime.now()` on Render does NOT fire peak surcharges an
    hour late.

    Accepts:
      - None → uses "now" in WAT
      - naive datetime → assumed UTC, converted to WAT
      - aware datetime → converted to WAT
    """
    if not order_time:
        order_time = datetime.now(LOCAL_TZ)
    elif order_time.tzinfo is None:
        # Naive input: assume UTC (matches Render and datetime.utcnow())
        order_time = order_time.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ)
    else:
        order_time = order_time.astimezone(LOCAL_TZ)

    dow = order_time.weekday()
    hour_min = order_time.strftime("%H:%M")
    for setting in peak_settings:
        if setting.get("day_of_week") is not None and setting["day_of_week"] != dow:
            continue
        start = setting.get("start_time")
        end = setting.get("end_time")
        if start and end:
            # TIME columns serialize as "HH:MM:SS"; trim to HH:MM for comparison
            start_hm = str(start)[:5]
            end_hm = str(end)[:5]
            if start_hm <= hour_min <= end_hm:
                return True
    return False


async def calculate_intelligent_delivery_fee(
    base_fee: int, order_value: int, items: List[OrderItem],
    order_time: Optional[datetime] = None, customer_email: Optional[str] = None
) -> Dict[str, Any]:
    fee = base_fee
    value_discount = 0
    item_surcharge = 0
    peak_surcharge = 0
    loyalty_discount = 0
    volume_surcharge = 0
    minimum_fee = getattr(settings, 'MINIMUM_DELIVERY_FEE', 500)

    product_ids = [item.product_id for item in items]
    db = get_supabase()
    try:
        result = await execute_db(
            db.table("products").select("id, is_main_item, weight_kg, is_bulky").in_("id", product_ids)
        )
        product_map = {p["id"]: p for p in result.data} if result.data else {}
    except Exception as e:
        logger.warning(f"Could not fetch product delivery details: {e}")
        product_map = {}

    main_count = 0
    total_weight = 0.0
    bulky_count = 0
    total_items = sum(item.qty for item in items)

    for item in items:
        pid = item.product_id
        prod = product_map.get(pid)
        if prod:
            is_main = prod.get("is_main_item")
            if is_main is None:
                is_main = True            
            if is_main:
                main_count += item.qty
            weight = prod.get("weight_kg")
            if weight is None:
                weight = 0.5
            total_weight += float(weight) * item.qty
            is_bulky = prod.get("is_bulky")
            if is_bulky is None:
                is_bulky = False
            if is_bulky:
                bulky_count += item.qty
        else:
            main_count += item.qty
            total_weight += 0.5 * item.qty

    try:
        thresholds = getattr(settings, 'VOLUME_SURCHARGE_THRESHOLDS', {})
        amounts = getattr(settings, 'VOLUME_SURCHARGE_AMOUNTS', {})
        if total_items >= thresholds.get("xxlarge", 10):
            volume_surcharge += amounts.get("xxlarge", 500)
        elif total_items >= thresholds.get("xlarge", 6):
            volume_surcharge += amounts.get("xlarge", 300)
        elif total_items >= thresholds.get("large", 4):
            volume_surcharge += amounts.get("large", 150)
    except Exception as e:
        logger.warning(f"Error calculating volume surcharge: {e}")

    try:
        weight_threshold = getattr(settings, 'WEIGHT_THRESHOLD_KG', 5.0)
        weight_surcharge_per_kg = getattr(settings, 'WEIGHT_SURCHARGE_PER_KG', 100)
        if total_weight > weight_threshold:
            extra_kg = total_weight - weight_threshold
            volume_surcharge += int(extra_kg * weight_surcharge_per_kg)
    except Exception as e:
        logger.warning(f"Error calculating weight surcharge: {e}")

    try:
        bulky_surcharge = getattr(settings, 'BULKY_ITEM_SURCHARGE', 200)
        if bulky_count > 0:
            volume_surcharge += bulky_count * bulky_surcharge
    except Exception as e:
        logger.warning(f"Error calculating bulky surcharge: {e}")

    try:
        surcharge_rules = await get_item_surcharge_rules()
        for rule in surcharge_rules:
            min_items = rule.get("min_main_items")
            max_items = rule.get("max_main_items")
            if (min_items is None or main_count >= min_items) and (max_items is None or main_count <= max_items):
                item_surcharge += rule.get("surcharge_amount", 0)
                break
        for rule in surcharge_rules:
            threshold = rule.get("weight_threshold_kg")
            per_kg = rule.get("surcharge_per_kg")
            if threshold is not None and per_kg is not None and total_weight > threshold:
                extra_kg = total_weight - threshold
                item_surcharge += int(extra_kg * per_kg)
                break
    except Exception as e:
        logger.warning(f"Error applying item surcharge rules: {e}")

    try:
        peak_settings = await get_peak_settings()
        peak_surcharge_amount = getattr(settings, 'PEAK_SURCHARGE', 200)
        if is_peak_hour(order_time, peak_settings):
            peak_surcharge = peak_surcharge_amount
    except Exception as e:
        logger.warning(f"Error calculating peak surcharge: {e}")

    try:
        if customer_email:
            loyalty_setting = await get_loyalty_setting()
            if loyalty_setting:
                min_orders = loyalty_setting.get("min_orders", 5)
                max_discount = getattr(settings, 'MAX_LOYALTY_DISCOUNT_PERCENT', 15)
                discount_pct = min(loyalty_setting.get("discount_percentage", 20), max_discount)
                count_result = await execute_db(
                    db.table("orders").select("id", count="exact")
                    .eq("customer_email", customer_email)
                    .in_("status", ["paid", "confirmed", "completed"])
                )
                order_count = count_result.count or 0
                if order_count >= min_orders and fee > 0:
                    loyalty_discount = int(fee * discount_pct / 100)
                    fee = fee - loyalty_discount
    except Exception as e:
        logger.warning(f"Error calculating loyalty discount: {e}")

    fee = fee + volume_surcharge + item_surcharge + peak_surcharge

    try:
        rules = await get_delivery_fee_rules()
        for rule in rules:
            min_val = rule.get("min_order_value", 0)
            max_val = rule.get("max_order_value")
            if order_value >= min_val and (max_val is None or order_value <= max_val):
                if rule.get("free_delivery", False):
                    value_discount = fee
                    fee = 0
                else:
                    multiplier = rule.get("fee_multiplier", 1.0)
                    discount_amount = rule.get("fee_discount", 0)
                    new_fee = fee * multiplier - discount_amount
                    value_discount = fee - new_fee
                    fee = max(0, new_fee)
                break
    except Exception as e:
        logger.warning(f"Error applying value discount rules: {e}")

    if fee > 0 and fee < minimum_fee:
        fee = minimum_fee

    final_fee = max(0, round(fee))
    value_discount = round(value_discount)
    item_surcharge = round(item_surcharge)
    peak_surcharge = round(peak_surcharge)
    loyalty_discount = round(loyalty_discount)
    volume_surcharge = round(volume_surcharge)

    return {
        "base_fee": base_fee,
        "volume_surcharge": volume_surcharge,
        "value_discount": value_discount,
        "item_surcharge": item_surcharge,
        "peak_surcharge": peak_surcharge,
        "loyalty_discount": loyalty_discount,
        "final_fee": final_fee,
        "breakdown": {
            "base": base_fee,
            "volume_surcharge": volume_surcharge,
            "item_surcharge": item_surcharge,
            "peak_surcharge": peak_surcharge,
            "loyalty_discount": -loyalty_discount,
            "value_discount": -value_discount,
            "final": final_fee
        }
    }

# ---------- DATABASE SETUP ----------
# Migration health is tracked so the schema state is observable via /health.
# Historically, failures here were swallowed with a warning, meaning the app
# could run for days against a stale schema before anything broke. Now every
# failure is logged at ERROR and the process reports itself as degraded.
_schema_state: Dict[str, Any] = {"ready": False, "errors": [], "last_run": None}

async def setup_database():
    db = get_supabase()
    errors: List[str] = []

    try:
        alter_queries = [
            "ALTER TABLE products ADD COLUMN IF NOT EXISTS is_main_item BOOLEAN DEFAULT TRUE;",
            "ALTER TABLE products ADD COLUMN IF NOT EXISTS weight_kg DECIMAL(4,2) DEFAULT 0.5;",
            "ALTER TABLE products ADD COLUMN IF NOT EXISTS is_bulky BOOLEAN DEFAULT FALSE;",
        ]
        for q in alter_queries:
            try:
                await execute_db(db.rpc("exec_sql", {"query": q}))
            except Exception as e:
                snippet = q.strip().splitlines()[0][:90]
                errors.append(f"ALTER {snippet}: {str(e)[:120]}")
                logger.error(
                    f"⚠️  SCHEMA MIGRATION FAILED (app may misbehave): {snippet}... → {e}. "
                    f"Run this manually in the Supabase SQL editor."
                )

        create_tables = [
            """
            CREATE TABLE IF NOT EXISTS delivery_fee_rules (
                id SERIAL PRIMARY KEY,
                min_order_value INTEGER NOT NULL,
                max_order_value INTEGER,
                fee_multiplier DECIMAL(3,2) DEFAULT 1.0,
                fee_discount INTEGER DEFAULT 0,
                free_delivery BOOLEAN DEFAULT FALSE,
                description TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS delivery_peak_settings (
                id SERIAL PRIMARY KEY,
                day_of_week INTEGER,
                start_time TIME,
                end_time TIME,
                surcharge_amount INTEGER DEFAULT 200,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS delivery_loyalty_settings (
                id SERIAL PRIMARY KEY,
                min_orders INTEGER DEFAULT 5,
                discount_percentage INTEGER DEFAULT 20,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS delivery_item_surcharge_rules (
                id SERIAL PRIMARY KEY,
                min_main_items INTEGER,
                max_main_items INTEGER,
                surcharge_amount INTEGER DEFAULT 0,
                weight_threshold_kg DECIMAL(5,2),
                surcharge_per_kg INTEGER,
                description TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
            """,
            """
        ]
        for sql in create_tables:
            try:
                await execute_db(db.rpc("exec_sql", {"query": sql}))
            except Exception as e:
                snippet = sql.strip().splitlines()[0][:90]
                errors.append(f"CREATE {snippet}: {str(e)[:120]}")
                logger.error(
                    f"⚠️  SCHEMA MIGRATION FAILED (app may misbehave): {snippet}... → {e}. "
                    f"Run this manually in the Supabase SQL editor."
                )

        _schema_state["ready"] = len(errors) == 0
        _schema_state["errors"] = errors
        _schema_state["last_run"] = datetime.now(timezone.utc).isoformat()

        if errors:
            logger.error(
                f"⚠️  Schema setup completed with {len(errors)} error(s). "
                f"Application may misbehave. /health will report 'degraded'."
            )
        else:
            logger.info("Runtime support tables ensured; order/payment schema is managed by versioned migrations.")

    except Exception as e:
        _schema_state["ready"] = False
        _schema_state["errors"] = [f"setup_database outer: {str(e)[:200]}"]
        _schema_state["last_run"] = datetime.now(timezone.utc).isoformat()
        logger.error(f"setup_database top-level failure: {e}", exc_info=True)

# =============================================
# ORDER RECONCILIATION
# =============================================
# Closes the gap where Monnify's webhook is lost: pending orders older than
# RECONCILIATION_GRACE_SECONDS get queried against Monnify, and any that are
# PAID get marked paid + stock-decremented using the same atomic-claim pattern
# as the webhook. FAILED/CANCELLED/EXPIRED/REVERSED orders get cancelled.
#
# [FEATURE] Reconciliation ONLY touches `pending` (online, at Monnify) orders.
# Offline orders in `awaiting_payment` are intentionally out of scope - those
# are legitimate orders waiting for a human to take payment at the counter.
_reconciliation_lock = asyncio.Lock()
_reconciliation_task: Optional[asyncio.Task] = None
_reconciliation_stats: Dict[str, Any] = {
    "last_run": None,
    "checked": 0,
    "resolved_paid": 0,
    "resolved_cancelled": 0,
    "errors": 0,
    # [FEATURE] Count of awaiting_payment orders flagged as no-show warnings
    "offline_no_show_warnings": 0,
}


async def _reconcile_once() -> None:
    """One pass. Called by the loop; also callable directly for tests/admin."""
    db = get_supabase()
    now = datetime.now(timezone.utc)
    grace_cutoff = (now - timedelta(seconds=settings.RECONCILIATION_GRACE_SECONDS)).isoformat()
    max_age_cutoff = (now - timedelta(hours=settings.RECONCILIATION_MAX_AGE_HOURS)).isoformat()

    # [FIX] Unconditional stale sweep. Any pending order older than
    # STALE_CHECKOUT_HOURS is cancelled regardless of Monnify's answer. This
    # is the backstop for orders where the in-window Monnify query kept
    # failing, and for orders where the customer abandoned the checkout long
    # enough ago that Monnify has since expired the transaction.
    stale_cancelled = 0
    try:
        stale_cutoff = (now - timedelta(hours=settings.STALE_CHECKOUT_HOURS)).isoformat()
        stale_result = await execute_db(
            db.table("orders")
            .select("id, payment_reference, created_at")
            .eq("status", "pending")
            .lt("created_at", stale_cutoff)
            .limit(200)
        )
        for stale in (stale_result.data or []):
            claim = await execute_db(
                db.table("orders")
                .update({
                    "status": "cancelled",
                    "cancellation_reason": (
                        f"Stale checkout auto-cancelled after "
                        f"{settings.STALE_CHECKOUT_HOURS}h"
                    ),
                })
                .eq("id", stale["id"])
                .eq("status", "pending")
            )
            if claim.data:
                stale_cancelled += 1
                logger.info(
                    f"Reconciliation: auto-cancelled stale checkout "
                    f"{stale['id']} (ref={stale.get('payment_reference')})"
                )
    except Exception as e:
        logger.warning(f"Stale checkout sweep failed: {e}")

    # [FEATURE] Warning-only sweep for offline orders that have been sitting in
    # `awaiting_payment` for a long time (likely no-shows). We do NOT auto-cancel
    # - a human should decide. But we log so staff know to chase.
    try:
        warn_cutoff = (now - timedelta(hours=settings.OFFLINE_NO_SHOW_WARNING_HOURS)).isoformat()
        warn_result = await execute_db(
            db.table("orders")
            .select("id, payment_reference, customer_name, customer_phone, created_at")
            .eq("status", "awaiting_payment")
            .lt("created_at", warn_cutoff)
            .limit(100)
        )
        for warn in (warn_result.data or []):
            _reconciliation_stats["offline_no_show_warnings"] += 1
            logger.warning(
                f"⚠️  Offline order {warn['id']} (ref={warn.get('payment_reference')}) "
                f"has been awaiting counter payment for over "
                f"{settings.OFFLINE_NO_SHOW_WARNING_HOURS}h - "
                f"customer={warn.get('customer_name')} phone={warn.get('customer_phone')}. "
                f"Consider following up or cancelling manually."
            )
    except Exception as e:
        logger.warning(f"Offline no-show warning sweep failed: {e}")

    try:
        result = await execute_db(
            db.table("orders")
            .select("id, payment_reference, monnify_transaction_ref, status, created_at")
            .eq("status", "pending")
            .lt("created_at", grace_cutoff)
            .gt("created_at", max_age_cutoff)
            .order("created_at", desc=False)
            .limit(settings.RECONCILIATION_BATCH_SIZE)
        )
    except Exception as e:
        _reconciliation_stats["errors"] += 1
        logger.error(f"Reconciliation: DB query failed: {e}")
        return

    orders = result.data or []
    if not orders:
        _reconciliation_stats["last_run"] = now.isoformat()
        if stale_cancelled:
            invalidate_stats_cache()
            _reconciliation_stats["resolved_cancelled"] += stale_cancelled
        return

    logger.info(f"Reconciliation: checking {len(orders)} pending order(s) against Monnify")
    monnify = get_monnify()
    checked = 0
    resolved_paid = 0
    resolved_cancelled = 0
    errors = 0

    for order in orders:
        txn_ref = order.get("monnify_transaction_ref")
        if not txn_ref:
            continue
        checked += 1
        resp = await monnify.query_transaction(txn_ref)
        if not resp.get("success"):
            errors += 1
            logger.warning(
                f"Reconciliation: query failed for order {order['id']} "
                f"(ref={txn_ref}): {resp.get('error')}"
            )
            continue

        body = resp.get("body", {})
        payment_status = (body.get("paymentStatus") or "").upper()
        amount_paid = body.get("amountPaid") or body.get("amount") or 0

        if payment_status in ("PAID", "OVERPAID"):
            if not payment_amount_matches(order.get("total"), amount_paid):
                errors += 1
                logger.error(
                    f"Reconciliation: payment amount mismatch for order {order['id']} "
                    f"(expected=₦{order.get('total')}, paid=₦{amount_paid}, "
                    f"status={payment_status})"
                )
                continue

            claim = await execute_db(
                db.rpc("transition_order_and_stock", {
                    "p_order_id": order["id"],
                    "p_from_status": "pending",
                    "p_to_status": "paid",
                })
            )
            if not claim.data:
                logger.info(f"Reconciliation: order {order['id']} already claimed - skipping")
                continue

            paid_order = claim.data[0]
            await get_brevo().send_order_confirmation(paid_order)
            logger.info(
                f"✅ Reconciliation: order {order['id']} marked PAID "
                f"(₦{amount_paid}, expected ₦{order.get('total')})"
            )
            resolved_paid += 1

        elif payment_status in ("FAILED", "CANCELLED", "EXPIRED", "REVERSED", "ABANDONED"):
            claim = await execute_db(
                db.table("orders")
                .update({
                    "status": "cancelled",
                    "cancellation_reason": f"Monnify reports {payment_status}",
                })
                .eq("id", order["id"])
                .eq("status", "pending")
            )
            if claim.data:
                logger.info(f"Reconciliation: order {order['id']} cancelled ({payment_status})")
                resolved_cancelled += 1

        else:
            # [FIX] Monnify reports PENDING or an unrecognised status. If the
            # order has been in this state past the abandoned-checkout grace
            # period, cancel it - the customer has clearly walked away.
            age_minutes = 0.0
            created_at_str = order.get("created_at")
            if created_at_str:
                try:
                    s = str(created_at_str).replace("Z", "+00:00")
                    created_at = datetime.fromisoformat(s)
                    if created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=timezone.utc)
                    age_minutes = (now - created_at).total_seconds() / 60.0
                except Exception:
                    age_minutes = 0.0
            if age_minutes >= settings.ABANDONED_CHECKOUT_GRACE_MINUTES:
                claim = await execute_db(
                    db.table("orders")
                    .update({
                        "status": "cancelled",
                        "cancellation_reason": (
                            f"Abandoned at payment gateway "
                            f"(Monnify: {payment_status or 'unknown'}, "
                            f"age: {int(age_minutes)} min)"
                        ),
                    })
                    .eq("id", order["id"])
                    .eq("status", "pending")
                )
                if claim.data:
                    logger.info(
                        f"Reconciliation: cancelled abandoned checkout {order['id']} "
                        f"(age={int(age_minutes)}min, Monnify={payment_status})"
                    )
                    resolved_cancelled += 1

    resolved_cancelled += stale_cancelled
    if resolved_paid or resolved_cancelled:
        invalidate_stats_cache()

    _reconciliation_stats["last_run"] = now.isoformat()
    _reconciliation_stats["checked"] += checked
    _reconciliation_stats["resolved_paid"] += resolved_paid
    _reconciliation_stats["resolved_cancelled"] += resolved_cancelled
    _reconciliation_stats["errors"] += errors

    if checked or stale_cancelled:
        logger.info(
            f"Reconciliation: done - checked={checked} "
            f"paid={resolved_paid} cancelled={resolved_cancelled} "
            f"(stale={stale_cancelled}) errors={errors}"
        )


async def reconcile_pending_orders():
    """Background loop. Started in lifespan; cancelled on shutdown."""
    logger.info(
        f"Reconciliation loop started (interval={settings.RECONCILIATION_INTERVAL_SECONDS}s, "
        f"grace={settings.RECONCILIATION_GRACE_SECONDS}s, "
        f"max_age={settings.RECONCILIATION_MAX_AGE_HOURS}h)"
    )
    # Small initial delay so startup migrations have time to run
    await asyncio.sleep(30)
    while True:
        try:
            if not _reconciliation_lock.locked():
                async with _reconciliation_lock:
                    await _reconcile_once()
        except asyncio.CancelledError:
            logger.info("Reconciliation loop cancelled")
            raise
        except Exception as e:
            logger.error(f"Reconciliation loop error: {e}", exc_info=True)
        try:
            await asyncio.sleep(settings.RECONCILIATION_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            logger.info("Reconciliation loop cancelled during sleep")
            raise


# ---------- LIFESPAN ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _reconciliation_task
    logger.info("Starting Hot Portion Grill - Full Monolith + AI + RBAC")

    if settings.SENTRY_DSN:
        if _SENTRY_AVAILABLE:
            try:
                sentry_sdk.init(
                    dsn=settings.SENTRY_DSN,
                    integrations=[FastApiIntegration()],
                    traces_sample_rate=0.1,
                    environment=os.getenv("RENDER_SERVICE_NAME", "production"),
                )
                logger.info("Sentry initialized")
            except Exception as e:
                logger.warning(f"Sentry init failed: {e}")
        else:
            logger.warning("SENTRY_DSN set but sentry_sdk not installed; skipping Sentry init")

    if not settings.SUPABASE_JWT_SECRET:
        logger.warning(
            "⚠️  SUPABASE_JWT_SECRET is NOT set. Authenticated endpoints will return 503. "
            "Set it from Supabase → Settings → API → JWT Secret."
        )

    # Warn if server-side geocoding isn't configured - deliveries will fall
    # back to Nominatim (or fail if the fallback is disabled).
    if not get_aws().geocoding_configured:
        logger.warning(
            "⚠️  AWS geocoding is NOT configured (AWS_LOCATION_API_KEY / AWS_GEOCODING_API_KEY). "
            "Server-side geocoding will rely on Nominatim - set AWS_LOCATION_API_KEY to fix."
        )

    # Surface the multi-worker hazard explicitly. The rate limiter and stats
    # cache live in process memory; under N workers each gets its own copy, so
    # effective limits become N× the configured value.
    try:
        web_concurrency = int(os.getenv("WEB_CONCURRENCY", "1") or "1")
    except ValueError:
        web_concurrency = 1
    if web_concurrency > 1:
        logger.warning(
            f"⚠️  WEB_CONCURRENCY={web_concurrency}. The rate limiter and stats cache are "
            f"in-memory (per-process) - effective rate limits and cache TTLs will be N× higher. "
            f"Run a single worker, or move both to Redis for multi-worker deployments."
        )

    init_results = await asyncio.gather(
        get_brevo().initialize(),
        get_monnify().initialize(),
        return_exceptions=True
    )
    for i, result in enumerate(init_results):
        if isinstance(result, Exception):
            logger.error(f"Init service {i} failed: {result}")

    asyncio.create_task(setup_database())

    if settings.RECONCILIATION_ENABLED:
        _reconciliation_task = asyncio.create_task(reconcile_pending_orders())
    else:
        logger.info("Reconciliation loop disabled (RECONCILIATION_ENABLED=false)")

    logger.info("All services ready (degraded mode allowed for external APIs)")
    try:
        yield
    finally:
        logger.info("Shutting down...")
        if _reconciliation_task and not _reconciliation_task.done():
            _reconciliation_task.cancel()
            try:
                await _reconciliation_task
            except asyncio.CancelledError:
                pass
        if _brevo and _brevo._session:
            await _brevo._session.close()
        if _monnify and _monnify._session:
            await _monnify._session.close()
        if _aws:
            await _aws.close()
        _executor.shutdown(wait=True)

# ---------- FASTAPI APP ----------
# [FIX] OpenAPI schema, Swagger UI, and ReDoc are disabled in production.
# They enumerate the full API surface (every route, every model), which is
# free reconnaissance for an attacker. Only exposed when DEBUG=True so a
# developer working locally or in staging can still use them.
app = FastAPI(
    title="Hot Portion Grill",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url=None,
    openapi_url="/openapi.json" if settings.DEBUG else None,
)

@app.middleware("http")
async def add_correlation_id(request: Request, call_next):
    cid = request.headers.get("X-Correlation-ID", f"{int(time.time() * 1000)}-{id(request)}")
    token = _correlation_id_var.set(cid)
    try:
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = cid
        return response
    finally:
        _correlation_id_var.reset(token)

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    cid = _correlation_id_var.get()
    logger.error(f"Unhandled: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "type": "internal-server-error",
            "title": "An unexpected error occurred",
            "status": 500,
            "trace_id": cid,
            "detail": str(exc) if settings.DEBUG else None
        }
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# [FIX] Baseline security headers on every response. The frontend is served
# by Netlify (which sets its own headers), but the backend still serves HTML
# at /docs (Swagger UI) when DEBUG=True, and may in future serve other
# HTML/JSON directly. This middleware guarantees a safe default regardless
# of what Netlify does.
#
# Notes:
#   * X-Content-Type-Options: nosniff - stops MIME-type sniffing.
#   * X-Frame-Options: DENY        - blocks framing (clickjacking).
#   * Referrer-Policy              - limits cross-origin referrer leakage.
#   * Permissions-Policy           - denies geolocation / camera / mic.
#   * X-Robots-Tag on /docs, /redoc, /openapi.json - belt-and-braces so
#     that even if docs are briefly enabled, search engines won't index them.
#
# We deliberately do NOT set a Content-Security-Policy here. Swagger UI needs
# a permissive inline-CSP to function, and the app's own HTML (index.html,
# admin.html) is served by Netlify, which is the right place for CSP. Adding
# a CSP here would be either useless or would break Swagger.
@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "geolocation=(), camera=(), microphone=()")
    if (
        request.url.path.startswith("/docs")
        or request.url.path.startswith("/redoc")
        or request.url.path.startswith("/openapi.json")
    ):
        response.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
    return response

# ---------- BANNER SERVICE ----------
class BannerService:
    def __init__(self):
        self.db = get_supabase()
        self.table = "banners"

    async def get_banners(self, is_active=None, is_hero=None, is_featured=None, category=None, limit=50, offset=0):
        query = self.db.table(self.table).select("*", count="exact")
        if is_active is not None:
            query = query.eq("is_active", is_active)
        if is_hero is not None:
            query = query.eq("is_hero", is_hero)
        if is_featured is not None:
            query = query.eq("is_featured", is_featured)
        count_res = await execute_db(query)
        total = count_res.count
        result = await execute_db(query.order("display_order").range(offset, offset + limit - 1))
        now = datetime.now().isoformat()
        filtered_data = []
        for banner in result.data:
            start = banner.get('start_date')
            end = banner.get('end_date')
            if start and start > now:
                continue
            if end and end < now:
                continue
            filtered_data.append(banner)
        for b in filtered_data:
            b['categories'] = await self._get_categories(b['id'])
            b['products'] = await self._get_products(b['id'])
        filtered_data = [convert_datetime_to_iso(b) for b in filtered_data]
        active_count = len([x for x in filtered_data if x.get('is_active', True)])
        hero_count = len([x for x in filtered_data if x.get('is_hero', False)])
        featured_count = len([x for x in filtered_data if x.get('is_featured', False)])
        return BannerResponse(
            banners=filtered_data, total=len(filtered_data),
            active_count=active_count, hero_count=hero_count, featured_count=featured_count
        )

    async def get_active(self, is_hero=None, is_featured=None):
        query = self.db.table(self.table).select("*").eq("is_active", True)
        if is_hero is not None:
            query = query.eq("is_hero", is_hero)
        if is_featured is not None:
            query = query.eq("is_featured", is_featured)
        result = await execute_db(query.order("display_order"))
        now = datetime.now().isoformat()
        filtered_data = []
        for banner in result.data:
            start = banner.get('start_date')
            end = banner.get('end_date')
            if start and start > now:
                continue
            if end and end < now:
                continue
            filtered_data.append(banner)
        for b in filtered_data:
            b['categories'] = await self._get_categories(b['id'])
            b['products'] = await self._get_products(b['id'])
        filtered_data = [convert_datetime_to_iso(b) for b in filtered_data]
        return filtered_data

    async def get_banner(self, banner_id: int):
        result = await execute_db(self.db.table(self.table).select("*").eq("id", banner_id))
        if not result.data:
            return None
        b = result.data[0]
        b['categories'] = await self._get_categories(b['id'])
        b['products'] = await self._get_products(b['id'])
        b = convert_datetime_to_iso(b)
        return b

    async def create_banner(self, banner: BannerCreate):
        data = banner.dict(exclude={'categories', 'products'})
        data["created_at"] = data["updated_at"] = datetime.now().isoformat()
        data = convert_datetime_to_iso(data)
        result = await execute_db(self.db.table(self.table).insert(data))
        if not result.data:
            raise HTTPException(400, "Create failed")
        bid = result.data[0]['id']
        if banner.categories:
            for cid in banner.categories:
                await execute_db(self.db.table("banner_categories").insert({"banner_id": bid, "category_id": cid}))
        if banner.products:
            for pid in banner.products:
                await execute_db(self.db.table("banner_products").insert({"banner_id": bid, "product_id": pid}))
        return result.data[0]

    async def update_banner(self, banner_id: int, banner: BannerUpdate):
        data = banner.dict(exclude={'categories', 'products'}, exclude_unset=True)
        data["updated_at"] = datetime.now().isoformat()
        data = convert_datetime_to_iso(data)
        result = await execute_db(self.db.table(self.table).update(data).eq("id", banner_id))
        if not result.data:
            raise HTTPException(404, "Not found")
        if banner.categories is not None:
            await execute_db(self.db.table("banner_categories").delete().eq("banner_id", banner_id))
            for cid in banner.categories:
                await execute_db(self.db.table("banner_categories").insert({"banner_id": banner_id, "category_id": cid}))
        if banner.products is not None:
            await execute_db(self.db.table("banner_products").delete().eq("banner_id", banner_id))
            for pid in banner.products:
                await execute_db(self.db.table("banner_products").insert({"banner_id": banner_id, "product_id": pid}))
        return result.data[0]

    async def delete_banner(self, banner_id: int):
        await execute_db(self.db.table("banner_categories").delete().eq("banner_id", banner_id))
        await execute_db(self.db.table("banner_products").delete().eq("banner_id", banner_id))
        result = await execute_db(self.db.table(self.table).delete().eq("id", banner_id))
        if not result.data:
            raise HTTPException(404, "Not found")

    async def toggle_banner(self, banner_id: int):
        b = await self.get_banner(banner_id)
        if not b:
            raise HTTPException(404, "Not found")
        new_status = not b['is_active']
        await execute_db(self.db.table(self.table).update({"is_active": new_status, "updated_at": datetime.now().isoformat()}).eq("id", banner_id))
        return {"id": banner_id, "is_active": new_status}

    async def duplicate_banner(self, banner_id: int):
        original = await self.get_banner(banner_id)
        if not original:
            raise HTTPException(404, "Not found")
        copy = {k: v for k, v in original.items() if k not in ['id', 'created_at', 'updated_at']}
        copy['title'] = f"{copy['title']} (Copy)"
        copy['is_active'] = False
        result = await execute_db(self.db.table(self.table).insert(copy))
        return result.data[0]

    async def reorder_banners(self, banner_ids: List[int]):
        for idx, bid in enumerate(banner_ids):
            await execute_db(self.db.table(self.table).update({"display_order": idx, "updated_at": datetime.now().isoformat()}).eq("id", bid))
        return {"success": True}

    async def _get_categories(self, bid):
        r = await execute_db(self.db.table("banner_categories").select("category_id").eq("banner_id", bid))
        return [x['category_id'] for x in r.data]

    async def _get_products(self, bid):
        r = await execute_db(self.db.table("banner_products").select("product_id").eq("banner_id", bid))
        return [x['product_id'] for x in r.data]

# =============================================
# PUBLIC API ROUTES
# =============================================

# [FIX] Split health check into public + authenticated.
#
# Public /health is a minimal liveness probe. It returns only enough to tell
# an uptime monitor whether the process is up - no version info, no schema
# state, no external-service configuration flags, no reconciliation counters.
# Those details are free reconnaissance for an attacker.
#
# /health/detail returns the full operational payload and requires an
# authenticated staff account. Operators can hit it with a bearer token when
# they need to debug migrations, geocoding, or reconciliation.
@app.get("/health")
async def health():
    """Public liveness probe. Minimal payload - no operational detail."""
    return {"status": "ok"}


@app.get("/health/detail")
async def health_detail(staff: Dict[str, Any] = Depends(get_current_staff)):
    """
    Authenticated readiness probe. Full operational detail for operators.

    Reports 'degraded' if schema migrations failed - wire this into internal
    alerting so silent schema drift doesn't go unnoticed.
    """
    return {
        "status": "healthy" if _schema_state["ready"] else "degraded",
        "schema_ready": _schema_state["ready"],
        "schema_errors": _schema_state["errors"][:5],  # cap to avoid huge payloads
        "schema_last_run": _schema_state["last_run"],
        "geocoding": {
            "aws_configured": get_aws().geocoding_configured,
            "nominatim_fallback_enabled": settings.NOMINATIM_FALLBACK_ENABLED,
            "cache_entries": len(_geocode_cache),
        },
        "reconciliation": {
            "enabled": settings.RECONCILIATION_ENABLED,
            "last_run": _reconciliation_stats["last_run"],
            "checked": _reconciliation_stats["checked"],
            "resolved_paid": _reconciliation_stats["resolved_paid"],
            "resolved_cancelled": _reconciliation_stats["resolved_cancelled"],
            "errors": _reconciliation_stats["errors"],
            "offline_no_show_warnings": _reconciliation_stats["offline_no_show_warnings"],
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

@app.get("/api/products", response_model=List[Product])
async def get_products():
    r = await execute_db(get_supabase().table("products").select("*").order("name"))
    return r.data

@app.get("/api/categories", response_model=List[Category])
async def get_categories():
    r = await execute_db(get_supabase().table("categories").select("*").order("name"))
    return r.data

@app.get("/api/top-products")
async def get_top_products(limit: int = 20):
    query = get_supabase().table("orders").select("items").in_("status", ["paid", "confirmed", "completed"])
    r = await execute_db(query)
    orders = r.data
    totals = {}
    for order in orders:
        for item in order.get('items', []):
            pid = item.get('product_id')
            if pid is None:
                continue
            qty = item.get('qty', 0)
            price = item.get('price', 0)
            if pid not in totals:
                totals[pid] = {'qty': 0, 'revenue': 0}
            totals[pid]['qty'] += qty
            totals[pid]['revenue'] += price * qty
    sorted_items = sorted(totals.items(), key=lambda x: x[1]['qty'], reverse=True)[:limit]
    product_ids = [pid for pid, _ in sorted_items]
    if product_ids:
        prod_res = await execute_db(
            get_supabase().table("products").select("id, name, price, emoji").in_("id", product_ids)
        )
        products_map = {p['id']: p for p in prod_res.data}
    else:
        products_map = {}
    result = []
    for pid, data in sorted_items:
        product_info = products_map.get(pid, {})
        result.append({
            "product_id": pid,
            "name": product_info.get('name', 'Unknown'),
            "emoji": product_info.get('emoji', '🍽️'),
            "quantity_sold": data['qty'],
            "revenue": data['revenue']
        })
    return result

# [FIX] Order lookup by reference now requires customer email.
# Rationale: a leaked reference (email footer, screenshot, browser history) is
# enough to fetch full customer PII - name, phone, address, items, total. Forcing
# an email match means an attacker needs both pieces, and the email is not
# derivable from the reference.
#
# NOTE: the customer frontend currently calls this endpoint without email and
# MUST be updated in the same deployment window. See deployment note at bottom.
@app.get("/api/orders/by-reference/{ref}")
async def get_order_by_reference(
    ref: str,
    email: str = Query(..., min_length=3, max_length=320, description="Customer email - must match the order"),
):
    if not ref or len(ref) > 128:
        raise HTTPException(400, "Invalid reference")
    email_norm = email.strip().lower()
    if not email_norm or "@" not in email_norm:
        raise HTTPException(400, "Invalid email")
    db = get_supabase()
    query = db.table("orders").select("*").eq("payment_reference", ref).eq("customer_email", email_norm)
    r = await execute_db(query.limit(1))
    if r.data:
        return r.data[0]
    query = db.table("orders").select("*").eq("monnify_transaction_ref", ref).eq("customer_email", email_norm)
    r = await execute_db(query.limit(1))
    if r.data:
        return r.data[0]
    raise HTTPException(404, "Order not found")


# [FIX] Serve an order that already exists for this idempotency key.
# Called from create_order when either the pre-check or the post-insert
# unique-violation handler detects an existing row for the incoming key.
#
# [FEATURE] Now also handles the offline case: an offline order replay returns
# the same "awaiting_payment" response instead of trying to fetch a Monnify
# checkout URL that was never created.
async def _serve_existing_order(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    If the existing row already has a checkout_url, return it - the customer
    is retrying the same intent and should land on the same Monnify page.

    If the row exists but checkout_url is NULL, a previous attempt died
    between our DB insert and Monnify init. Re-initialize Monnify with the
    same payment reference (Monnify treats this as idempotent) and store the
    resulting URL.
    """
    # [FEATURE] Offline orders never had a Monnify checkout. Return the same
    # shape as the original create request so the caller's client handles it
    # uniformly.
    if row.get("payment_method") == "offline":
        logger.info(
            f"Idempotent replay (offline): returning existing order {row.get('id')} "
            f"(ref={row.get('payment_reference')})"
        )
        return {
            "status": "awaiting_payment",
            "order_id": row.get("id"),
            "payment_reference": row.get("payment_reference"),
            "checkout_url": None,
            "payment_method": "offline",
            "delivery_method": row.get("delivery_method"),
            "idempotent_replay": True,
        }

    if row.get("checkout_url"):
        logger.info(
            f"Idempotent replay: returning existing order {row.get('id')} "
            f"(ref={row.get('payment_reference')})"
        )
        return {
            "status": "pending_payment",
            "order_id": row.get("id"),
            "payment_reference": row.get("payment_reference"),
            "checkout_url": row.get("checkout_url"),
            "idempotent_replay": True,
        }

    if row.get("status") != "pending":
        # The order moved past pending (paid/cancelled/confirmed). Client is
        # likely retrying a completed flow - send them to check their orders.
        raise HTTPException(
            status_code=409,
            detail=(
                f"An order with this request already exists (status: "
                f"{row.get('status')}). Please check your order history."
            ),
        )

    logger.warning(
        f"Idempotent replay without checkout_url: re-initializing Monnify for "
        f"order {row.get('id')} (ref={row.get('payment_reference')})"
    )
    monnify = get_monnify()
    monnify_result = await monnify.initialize_transaction(
        amount=int(row.get("total") or 0),
        customer_name=row.get("customer_name") or "",
        customer_email=row.get("customer_email") or "",
        customer_phone=row.get("customer_phone") or "",
        payment_reference=row.get("payment_reference") or "",
        payment_description="Hot Portion Grill Order",
    )
    if not monnify_result.get("success"):
        raise HTTPException(
            502,
            f"Payment initialization failed: {monnify_result.get('error', 'Unknown error')}",
        )

    await execute_db(
        get_supabase().table("orders")
        .update({
            "monnify_transaction_ref": monnify_result["transaction_reference"],
            "checkout_url": monnify_result["checkout_url"],
        })
        .eq("id", row.get("id"))
    )
    return {
        "status": "pending_payment",
        "order_id": row.get("id"),
        "payment_reference": row.get("payment_reference"),
        "checkout_url": monnify_result["checkout_url"],
        "idempotent_replay": True,
    }


@app.post("/api/orders", status_code=201)
async def create_order(order: OrderCreate, request: Request, bg: BackgroundTasks):
    await enforce_rate_limit(request, "orders", settings.RATE_LIMIT_ORDERS_PER_MINUTE)
    if not order.items:
        raise HTTPException(400, "Order must contain at least one item")
    product_ids = [item.product_id for item in order.items]
    if any(not pid for pid in product_ids):
        raise HTTPException(400, "All order items must have a product_id")

    # [FEATURE] Resolve payment method. Rules:
    #   - "online" or "offline" only.
    #   - Delivery orders MUST be online (we dispatch only after payment).
    #   - Pickup and dine-in support both.
    # Default is "online" so existing clients are unaffected.
    payment_method = (order.payment_method or "online").strip().lower()
    if payment_method not in ("online", "offline"):
        raise HTTPException(
            status_code=400,
            detail="payment_method must be 'online' or 'offline'",
        )
    delivery_method = (order.delivery_method or "pickup").strip().lower()
    if delivery_method == "delivery" and payment_method == "offline":
        raise HTTPException(
            status_code=400,
            detail=(
                "Delivery orders must be paid online at checkout. "
                "Choose online payment, or switch to Pickup / Dine-in "
                "to pay at the counter."
            ),
        )

    # [FIX] Idempotency key handling.
    # We only look up an existing order when the client supplied a key. If
    # they didn't, we generate one internally for record-keeping - but the
    # dedupe path is disabled for that request, preserving backward compat.
    idem_key_in = (order.idempotency_key or "").strip()
    if idem_key_in and len(idem_key_in) > 200:
        raise HTTPException(400, "idempotency_key too long (max 200 chars)")
    if idem_key_in:
        try:
            existing = await execute_db(
                get_supabase().table("orders")
                .select("id, payment_reference, monnify_transaction_ref, checkout_url, total, status, "
                        "customer_name, customer_email, customer_phone, "
                        "payment_method, delivery_method")
                .eq("idempotency_key", idem_key_in)
                .limit(1)
            )
        except Exception as e:
            logger.warning(f"Idempotency pre-check failed, proceeding: {e}")
            existing = None
        if existing and existing.data:
            return await _serve_existing_order(existing.data[0])

    stored_idem_key = idem_key_in or f"auto-{secrets.token_urlsafe(16)}"

    try:
        prod_result = await execute_db(
            get_supabase().table("products").select("id, name, price").in_("id", product_ids)
        )
    except Exception as e:
        logger.error(f"Failed to fetch products for order validation: {e}", exc_info=True)
        raise HTTPException(500, "Could not validate order items")
    product_map = {p["id"]: p for p in (prod_result.data or [])}
    missing = [pid for pid in product_ids if pid not in product_map]
    if missing:
        raise HTTPException(400, f"Invalid product id(s): {missing}")
    validated_items: List[Dict[str, Any]] = []
    computed_subtotal = 0
    for item in order.items:
        product = product_map[item.product_id]
        if item.qty <= 0:
            raise HTTPException(400, f"Invalid quantity for product {item.product_id}")
        server_price = int(product["price"])
        computed_subtotal += server_price * item.qty
        validated_items.append({
            "name": product["name"], "qty": item.qty,
            "price": server_price, "product_id": item.product_id,
        })

    # [FIX] FAIL-CLOSED delivery fee.
    # The server must compute the delivery fee itself, from the address and
    # its own pricing rules. If we cannot (geocoding failed, address outside
    # all zones, pricing service degraded), we REJECT the order with a clear
    # message - we never fall back to a client-supplied fee. The customer can
    # choose Pickup / Dine-in, or retry in a moment.
    computed_delivery_fee = 0
    if delivery_method == "delivery":
        if not order.delivery_address or not order.delivery_address.strip():
            raise HTTPException(400, "Delivery address is required for delivery orders")
        try:
            coords = await _resolve_delivery_coords(
                order.delivery_address,
                order.lat,
                order.lng,
                customer_email=order.customer_email,
            )
            if not coords:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "We couldn't locate that delivery address. "
                        "Please check the address, or choose Pickup / Dine-in."
                    ),
                )
            lat, lng = coords
            area_result = await execute_db(
                get_supabase().rpc("find_delivery_area", {"lat": lat, "lng": lng})
            )
            if not area_result.data:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "This address is outside our delivery area. "
                        "Please choose Pickup / Dine-in."
                    ),
                )
            base_fee = area_result.data[0].get("fee", 0)
            calc = await calculate_intelligent_delivery_fee(
                base_fee=base_fee,
                order_value=computed_subtotal,
                items=order.items,
                order_time=datetime.now(),
                customer_email=order.customer_email,
            )
            computed_delivery_fee = int(calc["final_fee"])
            logger.info(
                f"Server-computed delivery fee for '{order.delivery_address}': "
                f"₦{computed_delivery_fee} (base {base_fee})"
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(
                f"Delivery fee computation failed for '{order.delivery_address}' "
                f"(customer={order.customer_email}): {e}",
                exc_info=True,
            )
            raise HTTPException(
                status_code=400,
                detail=(
                    "We couldn't calculate the delivery fee right now. "
                    "Please choose Pickup / Dine-in, or try again in a moment."
                ),
            )

    server_payment_reference = f"HP-{secrets.token_urlsafe(16)}"
    computed_total = computed_subtotal + computed_delivery_fee

    # ------------------------------------------------------------------
    # [FEATURE] OFFLINE PATH - pickup or dine-in paid at the counter.
    # No Monnify. Order goes straight to `awaiting_payment`, which means
    # "customer is coming in to pay". The reconciliation loop never touches
    # this status; only a human (staff) can move it forward.
    # ------------------------------------------------------------------
    if payment_method == "offline":
        data = {
            "payment_reference": server_payment_reference,
            "customer_name": order.customer_name,
            "customer_email": order.customer_email,
            "customer_phone": order.customer_phone,
            "total": computed_total,
            "status": "awaiting_payment",
            "delivery_method": delivery_method,
            "payment_method": "offline",
            "delivery_address": None,
            "preferred_time": order.preferred_time,
            "order_notes": order.order_notes,
            "items": validated_items,
            "delivery_fee": 0,
            "delivery_breakdown": None,
            "idempotency_key": stored_idem_key,
            "checkout_url": None,
        }
        try:
            result = await execute_db(get_supabase().table("orders").insert(data))
        except Exception as e:
            # Race-safe idempotency (same pattern as the online path).
            err_str = str(e).lower()
            if idem_key_in and ("duplicate" in err_str or "unique" in err_str or "23505" in err_str):
                existing = await execute_db(
                    get_supabase().table("orders")
                    .select("id, payment_reference, monnify_transaction_ref, checkout_url, total, status, "
                            "customer_name, customer_email, customer_phone, "
                            "payment_method, delivery_method")
                    .eq("idempotency_key", idem_key_in)
                    .limit(1)
                )
                if existing.data:
                    logger.info(
                        f"Idempotency race won by concurrent request for key {idem_key_in} "
                        f"(offline); serving winning order {existing.data[0].get('id')}"
                    )
                    return await _serve_existing_order(existing.data[0])
            raise

        if not result.data:
            raise HTTPException(400, "Failed to create order")
        order_data = result.data[0]
        # Confirmation email fires immediately: the customer needs their order
        # reference right away, and the total is known. Delivery email is
        # scheduled in the background so the response isn't delayed by Brevo.
        bg.add_task(get_brevo().send_order_confirmation, order_data)
        logger.info(
            f"Offline order {order_data['id']} created "
            f"(customer={order.customer_email}, method={delivery_method}, "
            f"total=₦{computed_total}) - awaiting counter payment"
        )
        return {
            "status": "awaiting_payment",
            "order_id": order_data["id"],
            "payment_reference": server_payment_reference,
            "checkout_url": None,
            "payment_method": "offline",
            "delivery_method": delivery_method,
        }

    # ------------------------------------------------------------------
    # ONLINE PATH - existing behavior. Create order with `pending`, send
    # the customer to Monnify, and let the webhook / reconciliation move
    # it to `paid`.
    # ------------------------------------------------------------------
    data = {
        "payment_reference": server_payment_reference,
        "customer_name": order.customer_name,
        "customer_email": order.customer_email,
        "customer_phone": order.customer_phone,
        "total": computed_total,
        "status": "pending",
        "delivery_method": delivery_method,
        "payment_method": "online",
        "delivery_address": order.delivery_address,
        "preferred_time": order.preferred_time,
        "order_notes": order.order_notes,
        "items": validated_items,
        "delivery_fee": computed_delivery_fee,
        "delivery_breakdown": order.delivery_breakdown,
        "idempotency_key": stored_idem_key,
        "checkout_url": None,
    }
    try:
        result = await execute_db(get_supabase().table("orders").insert(data))
    except Exception as e:
        # [FIX] Race-safe idempotency: another request with the same key
        # squeezed in between our SELECT and our INSERT. The unique index on
        # idempotency_key caught it - serve the winning row.
        err_str = str(e).lower()
        if idem_key_in and ("duplicate" in err_str or "unique" in err_str or "23505" in err_str):
            existing = await execute_db(
                get_supabase().table("orders")
                .select("id, payment_reference, monnify_transaction_ref, checkout_url, total, status, "
                        "customer_name, customer_email, customer_phone, "
                        "payment_method, delivery_method")
                .eq("idempotency_key", idem_key_in)
                .limit(1)
            )
            if existing.data:
                logger.info(
                    f"Idempotency race won by concurrent request for key {idem_key_in}; "
                    f"serving winning order {existing.data[0].get('id')}"
                )
                return await _serve_existing_order(existing.data[0])
        raise

    if not result.data:
        raise HTTPException(400, "Failed to create order")
    order_data = result.data[0]
    monnify = get_monnify()
    monnify_result = await monnify.initialize_transaction(
        amount=computed_total,
        customer_name=order.customer_name,
        customer_email=order.customer_email,
        customer_phone=order.customer_phone,
        payment_reference=server_payment_reference,
        payment_description="Hot Portion Grill Order"
    )
    if not monnify_result["success"]:
        # Delete the row so a fresh attempt with the same key starts clean.
        try:
            await execute_db(get_supabase().table("orders").delete().eq("id", order_data["id"]))
        except Exception as del_err:
            logger.warning(f"Could not delete orphan order {order_data['id']}: {del_err}")
        raise HTTPException(400, f"Payment initialization failed: {monnify_result.get('error', 'Unknown error')}")
    # [FIX] Persist checkout_url alongside the Monnify transaction reference so
    # an idempotent replay can hand the customer back the same payment page.
    await execute_db(
        get_supabase().table("orders")
        .update({
            "monnify_transaction_ref": monnify_result["transaction_reference"],
            "checkout_url": monnify_result["checkout_url"],
        })
        .eq("id", order_data["id"])
    )
    # [FIX] Confirmation email is deliberately NOT sent here. At this point the
    # customer has only been given a Monnify checkout URL - they have not paid.
    # The email is triggered from the webhook handler and the reconciliation
    # loop, in both cases *after* the order has been atomically marked paid.
    # This prevents abandoned checkouts from generating false "Payment
    # Successful" emails.
    return {
        "status": "pending_payment",
        "order_id": order_data["id"],
        "payment_reference": server_payment_reference,
        "checkout_url": monnify_result["checkout_url"]
    }

@app.post("/api/delivery-fee", response_model=DeliveryFeeResponse)
async def get_delivery_fee(request: DeliveryFeeRequest, http_request: Request):
    await enforce_rate_limit(http_request, "delivery", settings.RATE_LIMIT_DELIVERY_PER_MINUTE)
    try:
        # [FIX] Same verification policy as order creation. Client coords are
        # only trusted if they match the server geocode of the typed address.
        coords = await _resolve_delivery_coords(
            request.address,
            request.lat,
            request.lng,
            customer_email=request.customer_email,
        )
        if not coords:
            return DeliveryFeeResponse(
                covered=False,
                message="Address not found. Please check the address and try again."
            )
        lat, lng = coords
        logger.info(f"Delivery fee for '{request.address}' resolved to lat={lat}, lng={lng}")
        db = get_supabase()
        result = await execute_db(db.rpc("find_delivery_area", {"lat": lat, "lng": lng}))
        if not result.data or len(result.data) == 0:
            return DeliveryFeeResponse(covered=False, message="Address not in any delivery area.")
        area = result.data[0]
        base_fee = area.get("fee", 0)
        area_name = area.get("name", "Unknown Area")
        order_time = request.order_time or datetime.now()
        calculation = await calculate_intelligent_delivery_fee(
            base_fee=base_fee, order_value=request.order_total, items=request.items,
            order_time=order_time, customer_email=request.customer_email
        )
        return DeliveryFeeResponse(
            covered=True, base_fee=base_fee, area_name=area_name,
            volume_surcharge=calculation.get("volume_surcharge", 0),
            value_discount=calculation["value_discount"],
            item_surcharge=calculation["item_surcharge"],
            peak_surcharge=calculation["peak_surcharge"],
            loyalty_discount=calculation["loyalty_discount"],
            total_fee=calculation["final_fee"],
            breakdown=calculation["breakdown"]
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in get_delivery_fee: {str(e)}", exc_info=True)
        raise HTTPException(500, "Error processing delivery fee request")

@app.get("/api/delivery-areas", response_model=List[DeliveryArea])
async def get_all_delivery_areas():
    try:
        db = get_supabase()
        result = await execute_db(db.rpc("get_delivery_areas_geojson", {}))
        areas = []
        for area_data in result.data:
            area_dict = dict(area_data)
            if "created_at" in area_dict and isinstance(area_dict["created_at"], (datetime, date)):
                area_dict["created_at"] = area_dict["created_at"].isoformat()
            if "updated_at" in area_dict and isinstance(area_dict["updated_at"], (datetime, date)):
                area_dict["updated_at"] = area_dict["updated_at"].isoformat()
            if "polygon" in area_dict and isinstance(area_dict["polygon"], str):
                area_dict["polygon"] = json.loads(area_dict["polygon"])
            areas.append(area_dict)
        return areas
    except Exception as e:
        logger.error(f"Error fetching delivery areas: {str(e)}", exc_info=True)
        raise HTTPException(500, "Error fetching delivery areas")

@app.get("/api/delivery-areas/point")
async def get_area_by_point(lat: float, lng: float):
    try:
        db = get_supabase()
        result = await execute_db(db.rpc("find_delivery_area", {"lat": lat, "lng": lng}))
        if result.data and len(result.data) > 0:
            return {"covered": True, "area": result.data[0]}
        else:
            return {"covered": False, "message": "Point not in any delivery area"}
    except Exception as e:
        logger.error(f"Error checking point: {str(e)}", exc_info=True)
        raise HTTPException(500, "Error checking delivery area coverage")

# Note: `reverse-geocode` removed - the server never reverse-geocodes, and the
# frontend shouldn't need it either. Keeping the surface small.
ALLOWED_PLACES_ENDPOINTS = {"autocomplete", "geocode","reverse-geocode"}

@app.post("/api/places/{endpoint}")
async def proxy_amazon_places(endpoint: str, body: Dict[str, Any], request: Request):
    await enforce_rate_limit(request, "places", settings.RATE_LIMIT_PLACES_PER_MINUTE)
    if endpoint not in ALLOWED_PLACES_ENDPOINTS:
        raise HTTPException(400, f"Invalid endpoint: {endpoint}")
    if not settings.AWS_LOCATION_API_KEY:
        logger.error("AWS_LOCATION_API_KEY not configured")
        raise HTTPException(500, "Amazon Location key not configured on server")
    url = f"https://places.geo.{settings.AWS_LOCATION_REGION}.amazonaws.com/v2/{endpoint}"
    params = {"key": settings.AWS_LOCATION_API_KEY}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.post(url, params=params, json=body) as resp:
                text = await resp.text()
                if resp.status != 200:
                    logger.warning(f"Amazon Places {endpoint} -> {resp.status}: {text[:200]}")
                try:
                    return JSONResponse(content=json.loads(text), status_code=resp.status)
                except json.JSONDecodeError:
                    raise HTTPException(502, f"Invalid response from Amazon: {text[:200]}")
    except asyncio.TimeoutError:
        raise HTTPException(504, "Amazon Places request timed out")
    except aiohttp.ClientError as e:
        logger.error(f"Amazon Places request failed: {e}")
        raise HTTPException(502, "Amazon Places unavailable")

@app.get("/api/v1/banners/active", response_model=List[Banner])
async def list_active_banners(is_hero: Optional[bool] = Query(None), is_featured: Optional[bool] = Query(None)):
    return await BannerService().get_active(is_hero, is_featured)

@app.get("/api/v1/banners/{banner_id}", response_model=Banner)
async def get_banner(banner_id: int):
    b = await BannerService().get_banner(banner_id)
    if not b:
        raise HTTPException(404, "Not found")
    return b

@app.post("/api/v1/webhooks/monnify")
async def monnify_webhook(
    request: Request,
    x_signature: Optional[str] = Header(None, alias="X-Signature")
):
    raw_body = await request.body()
    result = await get_monnify().handle_webhook(raw_body, x_signature)
    if not result["valid"]:
        logger.warning(f"Monnify webhook rejected: {result.get('error')}")
        raise HTTPException(400, f"Webhook validation failed: {result.get('error')}")
    if result.get("event") != "SUCCESSFUL_TRANSACTION":
        return {"status": "ignored", "event": result.get("event")}
    payload = result.get("payload", {})
    data = payload.get("data", {})
    trans_ref = data.get("transactionReference")
    payment_ref = data.get("paymentReference")
    query = get_supabase().table("orders").select("*")
    if payment_ref:
        query = query.eq("payment_reference", payment_ref)
    else:
        query = query.eq("monnify_transaction_ref", trans_ref)
    order_result = await execute_db(query)
    if not order_result.data:
        logger.warning(f"Order not found for ref: {payment_ref or trans_ref}")
        return {"status": "ignored"}
    order = order_result.data[0]
    if order.get("status") == "paid":
        logger.info(f"Webhook duplicate for order {order['id']} (already paid) - ignoring")
        return {"status": "already_processed"}

    paid_amount = data.get("amountPaid") or data.get("amount")
    if paid_amount is None or not payment_amount_matches(order.get("total"), paid_amount):
        logger.error(
            f"Monnify webhook amount mismatch for order {order['id']}: "
            f"expected=₦{order.get('total')}, paid=₦{paid_amount}"
        )
        return {"status": "amount_mismatch"}

    if order.get("status") != "pending":
        logger.warning(
            f"Monnify webhook ignored for order {order['id']} in state "
            f"'{order.get('status')}'"
        )
        return {"status": "ignored", "reason": "order_not_pending"}

    if (
        trans_ref
        and order.get("monnify_transaction_ref")
        and order.get("monnify_transaction_ref") != trans_ref
    ):
        logger.error(
            f"Monnify transaction reference mismatch for order {order['id']}: "
            f"stored={order.get('monnify_transaction_ref')} webhook={trans_ref}"
        )
        return {"status": "transaction_reference_mismatch"}

    claim_result = await execute_db(
        get_supabase().rpc("transition_order_and_stock", {
            "p_order_id": order["id"],
            "p_from_status": "pending",
            "p_to_status": "paid",
        })
    )
    if not claim_result.data:
        logger.info(f"Order {order['id']} already claimed by another worker - skipping stock decrement")
        return {"status": "already_processed"}

    paid_order = claim_result.data[0]
    await get_brevo().send_order_confirmation(paid_order)
    return {"status": "received"}

@app.post("/api/ai/chat", response_model=AIChatResponse)
async def chat_with_ai(req: AIChatRequest, request: Request, ai_service: AIService = Depends(get_ai_service)):
    await enforce_rate_limit(request, "ai", settings.RATE_LIMIT_AI_PER_MINUTE)
    result = await ai_service.chat(req.message)
    return AIChatResponse(response=result["response"], provider=result["provider"], model=result["model"])

@app.get("/api/ai/health")
async def ai_health(ai_service: AIService = Depends(get_ai_service)):
    return {
        "groq": ai_service.groq_client is not None,
        "gemini": ai_service.genai_client is not None,
        "openai": ai_service.openai_client is not None,
        "primary": settings.AI_PRIMARY,
        "fallback": settings.AI_FALLBACK
    }

# =============================================
# AUTHENTICATED ENDPOINTS (RBAC)
# =============================================

@app.get("/api/auth/me")
async def whoami(staff: Dict[str, Any] = Depends(get_current_staff)):
    """Return the currently-authenticated staff member's profile + permissions."""
    role = staff.get("role")
    perms = sorted(list(PERMISSIONS.get(role, set())))
    return {
        "id": staff["id"],
        "email": staff["email"],
        "full_name": staff["full_name"],
        "role": role,
        "is_active": staff.get("is_active", True),
        "permissions": perms,
    }

# ---------- STAFF MANAGEMENT (owner only) ----------

@app.get("/api/staff", response_model=List[StaffMember])
async def list_staff(staff: Dict[str, Any] = Depends(require_permission("staff:read"))):
    r = await execute_db(get_supabase().table("staff").select("*").order("created_at"))
    return r.data

@app.post("/api/staff", status_code=201)
async def create_staff(
    payload: StaffCreate,
    staff: Dict[str, Any] = Depends(require_permission("staff:write")),
    request: Request = None,
):
    """Create a new staff member via Supabase Auth Admin API with a preset password.

    [FIX] Replaces the invite-email flow. The admin chooses the initial
    password, shares it out-of-band (WhatsApp, in person, etc.), and the
    staff member can sign in immediately. Password changes later go through
    the "Forgot password" flow on the login screen.
    """
    email = payload.email.strip().lower()
    role = payload.role.strip().lower()
    if role not in ALLOWED_ROLES:
        raise HTTPException(400, f"Invalid role. Must be one of: {sorted(ALLOWED_ROLES)}")
    if role == "owner" and staff.get("role") != "owner":
        raise HTTPException(403, "Only an owner can create another owner")

    existing = await execute_db(get_supabase().table("staff").select("id").eq("email", email))
    if existing.data:
        raise HTTPException(400, "A staff member with this email already exists")

    # Create the auth user with a pre-set password. `email_confirm: true`
    # skips the confirmation email entirely, so the account is usable now.
    create_url = f"{settings.SUPABASE_URL}/auth/v1/admin/users"
    headers = {
        "apikey": settings.SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {settings.SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        async with session.post(
            create_url, headers=headers,
            json={
                "email": email,
                "password": payload.password,
                "email_confirm": True,
                "user_metadata": {
                    "full_name": payload.full_name,
                    "role": role,
                },
            }
        ) as resp:
            text = await resp.text()
            if resp.status not in (200, 201):
                logger.error(f"Supabase admin.create_user failed: {resp.status} {text[:200]}")
                # Detect the common "already exists" case and surface a
                # clear message for the admin.
                lower = text.lower()
                if "already" in lower and ("registered" in lower or "exists" in lower):
                    raise HTTPException(
                        400,
                        f"An auth account already exists for {email}. If the staff row "
                        f"was deleted but the auth user remains, remove it from the "
                        f"Supabase dashboard (Authentication → Users) and try again.",
                    )
                raise HTTPException(502, f"Failed to create user: {text[:200]}")
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                raise HTTPException(502, "Invalid response from Supabase create_user")
            user_id = data.get("id")
            if not user_id:
                raise HTTPException(500, "Supabase did not return a user id")

    row = {
        "id": user_id,
        "email": email,
        "full_name": payload.full_name,
        "role": role,
        "is_active": True,
    }
    r = await execute_db(get_supabase().table("staff").insert(row))
    if not r.data:
        # Roll back the auth user so we don't leave an orphan with a known
        # password and no staff row.
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                await session.delete(
                    f"{settings.SUPABASE_URL}/auth/v1/admin/users/{user_id}",
                    headers=headers,
                )
        except Exception as e:
            logger.warning(f"Failed to roll back auth user {user_id}: {e}")
        raise HTTPException(500, "Failed to create staff record")

    await audit_log(staff, "create", "staff", user_id, {"email": email, "role": role}, request)
    return r.data[0]

@app.patch("/api/staff/{staff_id}")
async def update_staff(
    staff_id: str,
    payload: StaffUpdate,
    staff: Dict[str, Any] = Depends(require_permission("staff:write")),
    request: Request = None,
):
    update_data: Dict[str, Any] = {}
    if payload.full_name is not None:
        update_data["full_name"] = payload.full_name
    if payload.role is not None:
        new_role = payload.role.strip().lower()
        if new_role not in ALLOWED_ROLES:
            raise HTTPException(400, f"Invalid role. Must be one of: {sorted(ALLOWED_ROLES)}")
        if new_role == "owner" and staff.get("role") != "owner":
            raise HTTPException(403, "Only an owner can promote someone to owner")
        # Prevent the last owner from being demoted
        if new_role != "owner":
            existing = await execute_db(
                get_supabase().table("staff").select("role").eq("id", staff_id)
            )
            if existing.data and existing.data[0].get("role") == "owner":
                owners = await execute_db(
                    get_supabase().table("staff").select("id", count="exact").eq("role", "owner")
                )
                if (owners.count or 0) <= 1:
                    raise HTTPException(400, "Cannot demote the last remaining owner")
        update_data["role"] = new_role

    if not update_data:
        raise HTTPException(400, "No fields to update")
    update_data["updated_at"] = datetime.now().isoformat()

    r = await execute_db(get_supabase().table("staff").update(update_data).eq("id", staff_id))
    if not r.data:
        raise HTTPException(404, "Staff member not found")
    await audit_log(staff, "update", "staff", staff_id, update_data, request)
    return r.data[0]

@app.delete("/api/staff/{staff_id}", status_code=200)
async def deactivate_staff(
    staff_id: str,
    staff: Dict[str, Any] = Depends(require_permission("staff:write")),
    request: Request = None,
):
    if staff_id == staff.get("id"):
        raise HTTPException(400, "You cannot deactivate your own account")
    existing = await execute_db(get_supabase().table("staff").select("*").eq("id", staff_id))
    if not existing.data:
        raise HTTPException(404, "Staff member not found")
    target = existing.data[0]
    if target.get("role") == "owner":
        owners = await execute_db(
            get_supabase().table("staff").select("id", count="exact").eq("role", "owner").eq("is_active", True)
        )
        if (owners.count or 0) <= 1:
            raise HTTPException(400, "Cannot deactivate the last remaining active owner")
    r = await execute_db(
        get_supabase().table("staff").update({
            "is_active": False,
            "updated_at": datetime.now().isoformat()
        }).eq("id", staff_id)
    )
    if not r.data:
        raise HTTPException(500, "Failed to deactivate staff member")
    await audit_log(staff, "deactivate", "staff", staff_id, None, request)
    return {"message": "Staff member deactivated", "id": staff_id, "is_active": False}

@app.post("/api/staff/{staff_id}/reactivate")
async def reactivate_staff(
    staff_id: str,
    staff: Dict[str, Any] = Depends(require_permission("staff:write")),
    request: Request = None,
):
    r = await execute_db(
        get_supabase().table("staff").update({
            "is_active": True,
            "updated_at": datetime.now().isoformat()
        }).eq("id", staff_id)
    )
    if not r.data:
        raise HTTPException(404, "Staff member not found")
    await audit_log(staff, "reactivate", "staff", staff_id, None, request)
    return r.data[0]

@app.get("/api/admin/audit-log")
async def get_audit_log(
    limit: int = 100,
    offset: int = 0,
    staff_id: Optional[str] = Query(None),
    resource_type: Optional[str] = Query(None),
    staff: Dict[str, Any] = Depends(require_permission("audit:read")),
):
    limit = max(1, min(limit, 500))
    query = get_supabase().table("staff_audit_log").select("*", count="exact")
    if staff_id:
        query = query.eq("staff_id", staff_id)
    if resource_type:
        query = query.eq("resource_type", resource_type)
    r = await execute_db(query.order("created_at", desc=True).range(offset, offset + limit - 1))
    return {"entries": r.data, "count": r.count}

# ---------- PRODUCTS (RBAC) ----------

@app.post("/api/products", response_model=Product, status_code=201)
async def create_product(
    p: ProductCreate,
    staff: Dict[str, Any] = Depends(require_permission("products:write")),
    request: Request = None,
):
    r = await execute_db(get_supabase().table("products").insert(p.dict()))
    if not r.data:
        raise HTTPException(400, "Failed")
    await audit_log(staff, "create", "product", r.data[0].get("id"), p.dict(), request)
    return r.data[0]

@app.put("/api/products/{pid}")
async def update_product(
    pid: int,
    p: ProductUpdate,
    staff: Dict[str, Any] = Depends(require_permission("products:write")),
    request: Request = None,
):
    r = await execute_db(get_supabase().table("products").update(p.dict()).eq("id", pid))
    if not r.data:
        raise HTTPException(404, "Not found")
    await audit_log(staff, "update", "product", pid, p.dict(), request)
    return r.data[0]

@app.delete("/api/products/{pid}", status_code=204)
async def delete_product(
    pid: int,
    staff: Dict[str, Any] = Depends(require_permission("products:write")),
    request: Request = None,
):
    r = await execute_db(get_supabase().table("products").delete().eq("id", pid))
    if not r.data:
        raise HTTPException(404, "Not found")
    await audit_log(staff, "delete", "product", pid, None, request)

# ---------- CATEGORIES (RBAC) ----------

@app.post("/api/categories", response_model=Category, status_code=201)
async def create_category(
    c: CategoryBase,
    staff: Dict[str, Any] = Depends(require_permission("categories:write")),
    request: Request = None,
):
    r = await execute_db(get_supabase().table("categories").insert(c.dict()))
    if not r.data:
        raise HTTPException(400, "Failed")
    await audit_log(staff, "create", "category", r.data[0].get("id"), c.dict(), request)
    return r.data[0]

@app.put("/api/categories/{cid}")
async def update_category(
    cid: int,
    c: CategoryBase,
    staff: Dict[str, Any] = Depends(require_permission("categories:write")),
    request: Request = None,
):
    r = await execute_db(get_supabase().table("categories").update(c.dict()).eq("id", cid))
    if not r.data:
        raise HTTPException(404, "Not found")
    await audit_log(staff, "update", "category", cid, c.dict(), request)
    return r.data[0]

@app.delete("/api/categories/{cid}", status_code=204)
async def delete_category(
    cid: int,
    staff: Dict[str, Any] = Depends(require_permission("categories:write")),
    request: Request = None,
):
    r = await execute_db(get_supabase().table("categories").delete().eq("id", cid))
    if not r.data:
        raise HTTPException(404, "Not found")
    await audit_log(staff, "delete", "category", cid, None, request)

# ---------- ORDERS (RBAC) ----------

# [FIX] Pagination on the orders list + pending hidden by default.
# Rationale: `pending` orders are checkout-in-progress (customer is currently
# at the payment gateway, or has abandoned it). They are not orders staff
# should act on. Reconciliation cancels abandoned ones after the grace period.
# Pass `include_pending=true` only for debugging.
#
# [FEATURE] `awaiting_payment` (offline, at-counter) orders ARE shown by
# default - those need staff attention to collect payment.
@app.get("/api/orders", response_model=List[Dict])
async def get_orders(
    since: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=1000, description="Max rows to return (default 200, max 1000)"),
    offset: int = Query(0, ge=0, description="Rows to skip for pagination"),
    include_pending: bool = Query(
        False,
        description=(
            "Include checkout-in-progress rows (status='pending'). Default "
            "false - those are customers currently at the payment gateway, "
            "not orders staff should act on. Set true only for debugging."
        ),
    ),
    staff: Dict[str, Any] = Depends(require_permission("orders:read")),
):
    query = get_supabase().table("orders").select("*").order("created_at", desc=True)
    if since:
        query = query.gt("created_at", since)
    if not include_pending:
        # [FIX] Hide checkout-in-progress orders from the default admin view.
        # This includes: fresh checkouts, abandoned checkouts that reconciliation
        # hasn't yet cleaned up, and any pending row still inside the grace window.
        # Staff should only see orders that need action.
        #
        # `awaiting_payment` (offline) is NOT filtered out - those need attention.
        query = query.neq("status", "pending")
    query = query.range(offset, offset + limit - 1)
    r = await execute_db(query)
    for o in r.data:
        o["itemCount"] = len(o.get("items", []))
    return r.data

@app.get("/api/orders/{oid}")
async def get_order(
    oid: int,
    staff: Dict[str, Any] = Depends(require_permission("orders:read")),
):
    r = await execute_db(get_supabase().table("orders").select("*").eq("id", oid))
    if not r.data:
        raise HTTPException(404, "Not found")
    return r.data[0]

@app.patch("/api/orders/{oid}/status")
async def update_order_status(
    oid: int,
    upd: OrderStatusUpdate,
    staff: Dict[str, Any] = Depends(require_permission("orders:update_status")),
    request: Request = None,
):
    order_result = await execute_db(get_supabase().table("orders").select("*").eq("id", oid))
    if not order_result.data:
        raise HTTPException(404, "Order not found")
    order = order_result.data[0]
    current_status = order.get("status")
    validate_order_transition(current_status, upd.status)
    update_data = {"status": upd.status}
    if upd.reason is not None:
        update_data["cancellation_reason"] = upd.reason
    # [FIX] Only restore stock if it was previously reduced. Orders in
    # 'pending' (checkout at Monnify) or 'awaiting_payment' (offline, no
    # money yet) never had stock reduced, so a cancel from those states must
    # NOT inflate stock. Stock is reduced when an order enters 'paid' or
    # 'confirmed', so those are the only states we reverse from.
    stock_affecting = (
        (upd.status in ("paid", "confirmed") and current_status not in ("paid", "confirmed"))
        or (upd.status == "cancelled" and current_status in ("paid", "confirmed"))
    )
    if stock_affecting:
        result = await execute_db(
            get_supabase().rpc("transition_order_and_stock", {
                "p_order_id": oid,
                "p_from_status": current_status,
                "p_to_status": upd.status,
                "p_cancellation_reason": upd.reason,
            })
        )
    else:
        result = await execute_db(
            get_supabase().table("orders")
            .update(update_data)
            .eq("id", oid)
            .eq("status", current_status)
        )

    if not result.data:
        raise HTTPException(
            status_code=409,
            detail="Order status changed by another request. Refresh and try again.",
        )
    invalidate_stats_cache()
    await audit_log(staff, "status_change", "order", oid, update_data, request)
    return result.data[0]

@app.post("/api/orders/{oid}/confirm-offline")
async def confirm_order_offline(
    oid: int,
    staff: Dict[str, Any] = Depends(require_permission("orders:update_status")),
    request: Request = None,
):
    order_result = await execute_db(get_supabase().table("orders").select("*").eq("id", oid))
    if not order_result.data:
        raise HTTPException(404, "Order not found")
    order = order_result.data[0]
    if order.get("status") in ("confirmed", "paid"):
        raise HTTPException(400, "Order already confirmed")
    validate_order_transition(order.get("status"), "confirmed")
    result = await execute_db(
        get_supabase().rpc("transition_order_and_stock", {
            "p_order_id": oid,
            "p_from_status": order.get("status"),
            "p_to_status": "confirmed",
        })
    )
    if not result.data:
        raise HTTPException(
            status_code=409,
            detail="Order was changed by another request. Refresh and try again.",
        )
    invalidate_stats_cache()
    await audit_log(staff, "confirm_offline", "order", oid, None, request)
    return {"message": "Order confirmed offline", "status": "confirmed"}

@app.get("/api/stats")
async def get_stats(staff: Dict[str, Any] = Depends(require_permission("stats:read"))):
    return await get_cached_stats()

# ---------- BANNERS (RBAC) ----------

@app.get("/api/v1/banners", response_model=BannerResponse)
async def list_banners(
    is_active: Optional[bool] = Query(None),
    is_hero: Optional[bool] = Query(None),
    is_featured: Optional[bool] = Query(None),
    category: Optional[str] = Query(None),
    limit: int = 50,
    offset: int = 0,
    staff: Dict[str, Any] = Depends(require_permission("banners:read")),
):
    return await BannerService().get_banners(is_active, is_hero, is_featured, category, limit, offset)

@app.post("/api/v1/banners", response_model=Banner, status_code=201)
async def create_banner(
    banner: BannerCreate,
    staff: Dict[str, Any] = Depends(require_permission("banners:write")),
    request: Request = None,
):
    created = await BannerService().create_banner(banner)
    await audit_log(staff, "create", "banner", created.get("id"), None, request)
    return created

@app.put("/api/v1/banners/{banner_id}", response_model=Banner)
async def update_banner(
    banner_id: int,
    banner: BannerUpdate,
    staff: Dict[str, Any] = Depends(require_permission("banners:write")),
    request: Request = None,
):
    updated = await BannerService().update_banner(banner_id, banner)
    await audit_log(staff, "update", "banner", banner_id, None, request)
    return updated

@app.delete("/api/v1/banners/{banner_id}", status_code=204)
async def delete_banner(
    banner_id: int,
    staff: Dict[str, Any] = Depends(require_permission("banners:write")),
    request: Request = None,
):
    await BannerService().delete_banner(banner_id)
    await audit_log(staff, "delete", "banner", banner_id, None, request)

@app.patch("/api/v1/banners/{banner_id}/toggle")
async def toggle_banner(
    banner_id: int,
    staff: Dict[str, Any] = Depends(require_permission("banners:write")),
    request: Request = None,
):
    result = await BannerService().toggle_banner(banner_id)
    await audit_log(staff, "toggle", "banner", banner_id, result, request)
    return result

@app.post("/api/v1/banners/{banner_id}/duplicate")
async def duplicate_banner(
    banner_id: int,
    staff: Dict[str, Any] = Depends(require_permission("banners:write")),
    request: Request = None,
):
    result = await BannerService().duplicate_banner(banner_id)
    await audit_log(staff, "duplicate", "banner", banner_id, {"new_id": result.get("id")}, request)
    return result

@app.patch("/api/v1/banners/reorder")
async def reorder_banners(
    banner_ids: List[int],
    staff: Dict[str, Any] = Depends(require_permission("banners:write")),
    request: Request = None,
):
    result = await BannerService().reorder_banners(banner_ids)
    await audit_log(staff, "reorder", "banner", None, {"order": banner_ids}, request)
    return result

# ---------- DELIVERY AREAS (RBAC) ----------

@app.post("/api/delivery-areas", response_model=DeliveryArea, status_code=201)
async def create_delivery_area(
    area: DeliveryAreaCreate,
    staff: Dict[str, Any] = Depends(require_permission("delivery_areas:write")),
    request: Request = None,
):
    try:
        if "type" not in area.polygon or area.polygon["type"] != "Polygon":
            raise HTTPException(400, "Invalid polygon: must be a GeoJSON Polygon")
        if "coordinates" not in area.polygon or not area.polygon["coordinates"]:
            raise HTTPException(400, "Invalid polygon: missing coordinates")
        coords = area.polygon["coordinates"][0]
        if len(coords) < 4:
            raise HTTPException(400, "Invalid polygon: must have at least 4 points")
        db = get_supabase()
        result = await execute_db(db.rpc("insert_delivery_area", {
            "_name": area.name, "_fee": area.fee, "_geojson": json.dumps(area.polygon)
        }))
        if not result.data:
            raise HTTPException(400, "Failed to create delivery area. Check that the polygon is valid.")
        created = dict(result.data[0])
        created["polygon"] = area.polygon
        if "created_at" in created and isinstance(created["created_at"], (datetime, date)):
            created["created_at"] = created["created_at"].isoformat()
        if "updated_at" in created and isinstance(created["updated_at"], (datetime, date)):
            created["updated_at"] = created["updated_at"].isoformat()
        await audit_log(staff, "create", "delivery_area", created.get("id"), {"name": area.name, "fee": area.fee}, request)
        return created
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating delivery area: {str(e)}", exc_info=True)
        raise HTTPException(500, f"Error creating delivery area: {str(e)}")

@app.put("/api/delivery-areas/{area_id}", response_model=DeliveryArea)
async def update_delivery_area(
    area_id: int,
    area: DeliveryAreaUpdate,
    staff: Dict[str, Any] = Depends(require_permission("delivery_areas:write")),
    request: Request = None,
):
    try:
        if "type" not in area.polygon or area.polygon["type"] != "Polygon":
            raise HTTPException(400, "Invalid polygon: must be a GeoJSON Polygon")
        if "coordinates" not in area.polygon or not area.polygon["coordinates"]:
            raise HTTPException(400, "Invalid polygon: missing coordinates")
        coords = area.polygon["coordinates"][0]
        if len(coords) < 4:
            raise HTTPException(400, "Invalid polygon: must have at least 4 points")
        db = get_supabase()
        result = await execute_db(db.rpc("update_delivery_area", {
            "_id": area_id, "_name": area.name, "_fee": area.fee,
            "_geojson": json.dumps(area.polygon)
        }))
        if not result.data:
            raise HTTPException(404, f"Delivery area with ID {area_id} not found")
        updated = dict(result.data[0])
        updated["polygon"] = area.polygon
        if "created_at" in updated and isinstance(updated["created_at"], (datetime, date)):
            updated["created_at"] = updated["created_at"].isoformat()
        if "updated_at" in updated and isinstance(updated["updated_at"], (datetime, date)):
            updated["updated_at"] = updated["updated_at"].isoformat()
        await audit_log(staff, "update", "delivery_area", area_id, {"name": area.name, "fee": area.fee}, request)
        return updated
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating delivery area: {str(e)}", exc_info=True)
        raise HTTPException(500, f"Error updating delivery area: {str(e)}")

@app.delete("/api/delivery-areas/{area_id}", status_code=204)
async def delete_delivery_area(
    area_id: int,
    staff: Dict[str, Any] = Depends(require_permission("delivery_areas:write")),
    request: Request = None,
):
    try:
        db = get_supabase()
        check_result = await execute_db(db.table("delivery_areas").select("id").eq("id", area_id))
        if not check_result.data:
            raise HTTPException(404, f"Delivery area with ID {area_id} not found")
        await execute_db(db.table("delivery_areas").delete().eq("id", area_id))
        await audit_log(staff, "delete", "delivery_area", area_id, None, request)
        return None
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting delivery area: {str(e)}", exc_info=True)
        raise HTTPException(500, f"Error deleting delivery area: {str(e)}")

# =============================================
# ADMIN ENDPOINTS FOR DELIVERY RULES & SETTINGS
# =============================================

# ---- VALUE DISCOUNT RULES ----
@app.get("/api/admin/delivery-rules", response_model=List[DeliveryFeeRule])
async def get_delivery_rules(staff: Dict[str, Any] = Depends(require_permission("delivery_rules:read"))):
    db = get_supabase()
    result = await execute_db(db.table("delivery_fee_rules").select("*").order("min_order_value"))
    return result.data

@app.get("/api/admin/delivery-rules/{rule_id}", response_model=DeliveryFeeRule)
async def get_delivery_rule(rule_id: int, staff: Dict[str, Any] = Depends(require_permission("delivery_rules:read"))):
    db = get_supabase()
    result = await execute_db(db.table("delivery_fee_rules").select("*").eq("id", rule_id))
    if not result.data:
        raise HTTPException(404, "Rule not found")
    return result.data[0]

@app.post("/api/admin/delivery-rules", response_model=DeliveryFeeRule, status_code=201)
async def create_delivery_rule(
    rule: DeliveryFeeRuleCreate,
    staff: Dict[str, Any] = Depends(require_permission("delivery_rules:write")),
    request: Request = None,
):
    db = get_supabase()
    data = rule.dict(exclude={'id', 'created_at', 'updated_at'})
    data["created_at"] = data["updated_at"] = datetime.now().isoformat()
    result = await execute_db(db.table("delivery_fee_rules").insert(data))
    if not result.data:
        raise HTTPException(400, "Failed to create rule")
    await audit_log(staff, "create", "delivery_rule", result.data[0].get("id"), data, request)
    return result.data[0]

@app.put("/api/admin/delivery-rules/{rule_id}", response_model=DeliveryFeeRule)
async def update_delivery_rule(
    rule_id: int,
    rule: DeliveryFeeRuleUpdate,
    staff: Dict[str, Any] = Depends(require_permission("delivery_rules:write")),
    request: Request = None,
):
    db = get_supabase()
    data = rule.dict(exclude={'id', 'created_at', 'updated_at'}, exclude_unset=True)
    data["updated_at"] = datetime.now().isoformat()
    result = await execute_db(db.table("delivery_fee_rules").update(data).eq("id", rule_id))
    if not result.data:
        raise HTTPException(404, "Rule not found")
    await audit_log(staff, "update", "delivery_rule", rule_id, data, request)
    return result.data[0]

@app.delete("/api/admin/delivery-rules/{rule_id}", status_code=204)
async def delete_delivery_rule(
    rule_id: int,
    staff: Dict[str, Any] = Depends(require_permission("delivery_rules:write")),
    request: Request = None,
):
    db = get_supabase()
    result = await execute_db(db.table("delivery_fee_rules").delete().eq("id", rule_id))
    if not result.data:
        raise HTTPException(404, "Rule not found")
    await audit_log(staff, "delete", "delivery_rule", rule_id, None, request)

# ---- PEAK SETTINGS ----
@app.get("/api/admin/peak-settings", response_model=List[DeliveryPeakSetting])
async def get_peak_settings_admin(staff: Dict[str, Any] = Depends(require_permission("delivery_rules:read"))):
    db = get_supabase()
    result = await execute_db(db.table("delivery_peak_settings").select("*").order("day_of_week"))
    return result.data

@app.get("/api/admin/peak-settings/{setting_id}", response_model=DeliveryPeakSetting)
async def get_peak_setting_admin(setting_id: int, staff: Dict[str, Any] = Depends(require_permission("delivery_rules:read"))):
    db = get_supabase()
    result = await execute_db(db.table("delivery_peak_settings").select("*").eq("id", setting_id))
    if not result.data:
        raise HTTPException(404, "Peak setting not found")
    return result.data[0]

@app.post("/api/admin/peak-settings", response_model=DeliveryPeakSetting, status_code=201)
async def create_peak_setting_admin(
    setting: DeliveryPeakSettingCreate,
    staff: Dict[str, Any] = Depends(require_permission("delivery_rules:write")),
    request: Request = None,
):
    db = get_supabase()
    data = setting.dict(exclude={'id', 'created_at', 'updated_at'})
    data["created_at"] = data["updated_at"] = datetime.now().isoformat()
    result = await execute_db(db.table("delivery_peak_settings").insert(data))
    if not result.data:
        raise HTTPException(400, "Failed to create peak setting")
    await audit_log(staff, "create", "peak_setting", result.data[0].get("id"), data, request)
    return result.data[0]

@app.put("/api/admin/peak-settings/{setting_id}", response_model=DeliveryPeakSetting)
async def update_peak_setting_admin(
    setting_id: int,
    setting: DeliveryPeakSettingUpdate,
    staff: Dict[str, Any] = Depends(require_permission("delivery_rules:write")),
    request: Request = None,
):
    db = get_supabase()
    data = setting.dict(exclude={'id', 'created_at', 'updated_at'}, exclude_unset=True)
    data["updated_at"] = datetime.now().isoformat()
    result = await execute_db(db.table("delivery_peak_settings").update(data).eq("id", setting_id))
    if not result.data:
        raise HTTPException(404, "Peak setting not found")
    await audit_log(staff, "update", "peak_setting", setting_id, data, request)
    return result.data[0]

@app.delete("/api/admin/peak-settings/{setting_id}", status_code=204)
async def delete_peak_setting_admin(
    setting_id: int,
    staff: Dict[str, Any] = Depends(require_permission("delivery_rules:write")),
    request: Request = None,
):
    db = get_supabase()
    result = await execute_db(db.table("delivery_peak_settings").delete().eq("id", setting_id))
    if not result.data:
        raise HTTPException(404, "Peak setting not found")
    await audit_log(staff, "delete", "peak_setting", setting_id, None, request)

# ---- LOYALTY SETTINGS ----
@app.get("/api/admin/loyalty-settings", response_model=List[DeliveryLoyaltySetting])
async def get_loyalty_settings_admin(staff: Dict[str, Any] = Depends(require_permission("delivery_rules:read"))):
    db = get_supabase()
    result = await execute_db(db.table("delivery_loyalty_settings").select("*"))
    return result.data

@app.get("/api/admin/loyalty-settings/{setting_id}", response_model=DeliveryLoyaltySetting)
async def get_loyalty_setting_admin(setting_id: int, staff: Dict[str, Any] = Depends(require_permission("delivery_rules:read"))):
    db = get_supabase()
    result = await execute_db(db.table("delivery_loyalty_settings").select("*").eq("id", setting_id))
    if not result.data:
        raise HTTPException(404, "Loyalty setting not found")
    return result.data[0]

@app.post("/api/admin/loyalty-settings", response_model=DeliveryLoyaltySetting, status_code=201)
async def create_loyalty_setting_admin(
    setting: DeliveryLoyaltySettingCreate,
    staff: Dict[str, Any] = Depends(require_permission("delivery_rules:write")),
    request: Request = None,
):
    db = get_supabase()
    data = setting.dict(exclude={'id', 'created_at', 'updated_at'})
    data["created_at"] = data["updated_at"] = datetime.now().isoformat()
    result = await execute_db(db.table("delivery_loyalty_settings").insert(data))
    if not result.data:
        raise HTTPException(400, "Failed to create loyalty setting")
    await audit_log(staff, "create", "loyalty_setting", result.data[0].get("id"), data, request)
    return result.data[0]

@app.put("/api/admin/loyalty-settings/{setting_id}", response_model=DeliveryLoyaltySetting)
async def update_loyalty_setting_admin(
    setting_id: int,
    setting: DeliveryLoyaltySettingUpdate,
    staff: Dict[str, Any] = Depends(require_permission("delivery_rules:write")),
    request: Request = None,
):
    db = get_supabase()
    data = setting.dict(exclude={'id', 'created_at', 'updated_at'}, exclude_unset=True)
    data["updated_at"] = datetime.now().isoformat()
    result = await execute_db(db.table("delivery_loyalty_settings").update(data).eq("id", setting_id))
    if not result.data:
        raise HTTPException(404, "Loyalty setting not found")
    await audit_log(staff, "update", "loyalty_setting", setting_id, data, request)
    return result.data[0]

@app.delete("/api/admin/loyalty-settings/{setting_id}", status_code=204)
async def delete_loyalty_setting_admin(
    setting_id: int,
    staff: Dict[str, Any] = Depends(require_permission("delivery_rules:write")),
    request: Request = None,
):
    db = get_supabase()
    result = await execute_db(db.table("delivery_loyalty_settings").delete().eq("id", setting_id))
    if not result.data:
        raise HTTPException(404, "Loyalty setting not found")
    await audit_log(staff, "delete", "loyalty_setting", setting_id, None, request)

# ---- ITEM SURCHARGE RULES ----
@app.get("/api/admin/item-surcharge-rules", response_model=List[DeliveryItemSurchargeRule])
async def get_item_surcharge_rules_admin(staff: Dict[str, Any] = Depends(require_permission("delivery_rules:read"))):
    db = get_supabase()
    result = await execute_db(db.table("delivery_item_surcharge_rules").select("*").order("min_main_items"))
    return result.data

@app.get("/api/admin/item-surcharge-rules/{rule_id}", response_model=DeliveryItemSurchargeRule)
async def get_item_surcharge_rule_admin(rule_id: int, staff: Dict[str, Any] = Depends(require_permission("delivery_rules:read"))):
    db = get_supabase()
    result = await execute_db(db.table("delivery_item_surcharge_rules").select("*").eq("id", rule_id))
    if not result.data:
        raise HTTPException(404, "Rule not found")
    return result.data[0]

@app.post("/api/admin/item-surcharge-rules", response_model=DeliveryItemSurchargeRule, status_code=201)
async def create_item_surcharge_rule_admin(
    rule: DeliveryItemSurchargeRuleCreate,
    staff: Dict[str, Any] = Depends(require_permission("delivery_rules:write")),
    request: Request = None,
):
    db = get_supabase()
    data = rule.dict(exclude={'id', 'created_at', 'updated_at'})
    data["created_at"] = data["updated_at"] = datetime.now().isoformat()
    result = await execute_db(db.table("delivery_item_surcharge_rules").insert(data))
    if not result.data:
        raise HTTPException(400, "Failed to create item surcharge rule")
    await audit_log(staff, "create", "item_surcharge_rule", result.data[0].get("id"), data, request)
    return result.data[0]

@app.put("/api/admin/item-surcharge-rules/{rule_id}", response_model=DeliveryItemSurchargeRule)
async def update_item_surcharge_rule_admin(
    rule_id: int,
    rule: DeliveryItemSurchargeRuleUpdate,
    staff: Dict[str, Any] = Depends(require_permission("delivery_rules:write")),
    request: Request = None,
):
    db = get_supabase()
    data = rule.dict(exclude={'id', 'created_at', 'updated_at'}, exclude_unset=True)
    data["updated_at"] = datetime.now().isoformat()
    result = await execute_db(db.table("delivery_item_surcharge_rules").update(data).eq("id", rule_id))
    if not result.data:
        raise HTTPException(404, "Rule not found")
    await audit_log(staff, "update", "item_surcharge_rule", rule_id, data, request)
    return result.data[0]

@app.delete("/api/admin/item-surcharge-rules/{rule_id}", status_code=204)
async def delete_item_surcharge_rule_admin(
    rule_id: int,
    staff: Dict[str, Any] = Depends(require_permission("delivery_rules:write")),
    request: Request = None,
):
    db = get_supabase()
    result = await execute_db(db.table("delivery_item_surcharge_rules").delete().eq("id", rule_id))
    if not result.data:
        raise HTTPException(404, "Rule not found")
    await audit_log(staff, "delete", "item_surcharge_rule", rule_id, None, request)

# ---------- RUN ----------
# NOTE ON SCALING:
# In-memory caches + rate limiter are per-process. To scale workers >1, move
# to Redis. For a single small restaurant instance this is not a concern.
# Startup logs a loud warning if WEB_CONCURRENCY > 1.
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, workers=1, log_level="info")
