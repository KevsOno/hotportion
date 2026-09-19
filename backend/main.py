import asyncio
import json
import os
import hmac
import hashlib
import base64
import logging
import time
import re
import secrets
import contextvars
from datetime import datetime, timedelta, date, timezone
from typing import List, Optional, Dict, Any, Set
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
    AWS_LOCATION_REGION: str = "eu-north-1"
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
# must swap this for a shared store (e.g. Redis) — otherwise each worker enforces
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
    """Record an admin mutation. Never raises — a failed audit must not break the operation."""
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
_ALLOWED_ORDER_TRANSITIONS: Dict[str, Set[str]] = {
    "pending":   {"paid", "confirmed", "cancelled"},
    "paid":      {"confirmed", "completed", "cancelled"},
    "confirmed": {"completed", "cancelled"},
    "cancelled": set(),
    "completed": set(),
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
        logger.warning(f"Unknown current order status '{current}', allowing transition to '{target}'")
        return
    if target_lc not in allowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status transition: {current} → {target}"
        )

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
    delivery_address: Optional[str] = None
    preferred_time: Optional[str] = None
    order_notes: Optional[str] = None
    items: List[OrderItem]
    monnify_transaction_ref: Optional[str] = None
    delivery_fee: Optional[int] = 0
    delivery_breakdown: Optional[Dict[str, Any]] = None

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
            html = f"""
            <html><body>
            <h2>Order #{data['payment_reference']}</h2>
            <p><strong>Customer:</strong> {data['customer_name']}</p>
            <p><strong>Phone:</strong> {data['customer_phone']}</p>
            <p><strong>Delivery:</strong> {data.get('delivery_method', 'Pickup')}</p>
            <p><strong>Delivery Fee:</strong> ₦{data.get('delivery_fee', 0):,}</p>
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
            "redirectUrl": "https://hotportion.netlify.app/?status=success",
            "webhookUrl": "https://hotportion.onrender.com/api/v1/webhooks/monnify",
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

# ---------- SINGLETONS ----------
_brevo = None
_monnify = None
_ai_service = None

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
        execute_db(db.table("orders").select("id", count="exact")),
        execute_db(db.table("orders").select("total"))
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

# =============================================
# STOCK MANAGEMENT HELPERS
# =============================================

async def _decrement_stock_atomic(product_id: int, qty: int) -> bool:
    try:
        await execute_db(get_supabase().rpc("decrement_product_stock", {"p_product_id": product_id, "p_qty": qty}))
        return True
    except Exception as e:
        logger.debug(f"Atomic decrement RPC unavailable, will fall back: {e}")
        return False

async def _increment_stock_atomic(product_id: int, qty: int) -> bool:
    try:
        await execute_db(get_supabase().rpc("increment_product_stock", {"p_product_id": product_id, "p_qty": qty}))
        return True
    except Exception as e:
        logger.debug(f"Atomic increment RPC unavailable, will fall back: {e}")
        return False

async def reduce_order_stock(order: dict):
    items = order.get("items", [])
    for item in items:
        product_id = item.get("product_id")
        qty = item.get("qty", 0)
        if not product_id or qty <= 0:
            continue
        used_atomic = await _decrement_stock_atomic(product_id, qty)
        if not used_atomic:
            prod_result = await execute_db(get_supabase().table("products").select("stock").eq("id", product_id))
            if prod_result.data:
                current_stock = prod_result.data[0].get("stock", 0)
                new_stock = max(0, current_stock - qty)
                await execute_db(
                    get_supabase().table("products").update({"stock": new_stock}).eq("id", product_id)
                )
                logger.info(f"Stock updated for product {product_id}: {current_stock} → {new_stock}")
    invalidate_stats_cache()

async def restore_order_stock(order: dict):
    items = order.get("items", [])
    for item in items:
        product_id = item.get("product_id")
        qty = item.get("qty", 0)
        if not product_id or qty <= 0:
            continue
        used_atomic = await _increment_stock_atomic(product_id, qty)
        if not used_atomic:
            prod_result = await execute_db(get_supabase().table("products").select("stock").eq("id", product_id))
            if prod_result.data:
                current_stock = prod_result.data[0].get("stock", 0)
                new_stock = current_stock + qty
                await execute_db(
                    get_supabase().table("products").update({"stock": new_stock}).eq("id", product_id)
                )
                logger.info(f"Stock restored for product {product_id}: {current_stock} → {new_stock}")
    invalidate_stats_cache()

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
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivery_fee INTEGER DEFAULT 0;",
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS cancellation_reason TEXT;",
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivery_breakdown JSONB;",
            "CREATE INDEX IF NOT EXISTS idx_orders_payment_reference ON orders (payment_reference);",
            "CREATE INDEX IF NOT EXISTS idx_orders_monnify_transaction_ref ON orders (monnify_transaction_ref);",
            # Reconciliation loop queries: pending orders by age
            "CREATE INDEX IF NOT EXISTS idx_orders_status_created_at ON orders (status, created_at);"
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
            CREATE OR REPLACE FUNCTION decrement_product_stock(p_product_id INTEGER, p_qty INTEGER)
            RETURNS VOID AS $$
            BEGIN
                UPDATE products SET stock = GREATEST(0, stock - p_qty) WHERE id = p_product_id;
            END;
            $$ LANGUAGE plpgsql;
            """,
            """
            CREATE OR REPLACE FUNCTION increment_product_stock(p_product_id INTEGER, p_qty INTEGER)
            RETURNS VOID AS $$
            BEGIN
                UPDATE products SET stock = stock + p_qty WHERE id = p_product_id;
            END;
            $$ LANGUAGE plpgsql;
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
            logger.info("Database indexes, columns, tables, and RPC functions ensured.")

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
_reconciliation_lock = asyncio.Lock()
_reconciliation_task: Optional[asyncio.Task] = None
_reconciliation_stats: Dict[str, Any] = {
    "last_run": None,
    "checked": 0,
    "resolved_paid": 0,
    "resolved_cancelled": 0,
    "errors": 0,
}


async def _reconcile_once() -> None:
    """One pass. Called by the loop; also callable directly for tests/admin."""
    db = get_supabase()
    now = datetime.now(timezone.utc)
    grace_cutoff = (now - timedelta(seconds=settings.RECONCILIATION_GRACE_SECONDS)).isoformat()
    max_age_cutoff = (now - timedelta(hours=settings.RECONCILIATION_MAX_AGE_HOURS)).isoformat()

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
            # Atomic claim: only transition if still pending (matches webhook)
            claim = await execute_db(
                db.table("orders")
                .update({"status": "paid"})
                .eq("id", order["id"])
                .eq("status", "pending")
            )
            if not claim.data:
                logger.info(f"Reconciliation: order {order['id']} already claimed — skipping")
                continue
            # Re-fetch to get full items list for stock decrement
            full = await execute_db(db.table("orders").select("*").eq("id", order["id"]))
            if full.data:
                await reduce_order_stock(full.data[0])
            logger.info(f"✅ Reconciliation: order {order['id']} marked PAID (₦{amount_paid})")
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

        # else: PENDING / PARTIALLY_PAID / anything else → leave alone

    if resolved_paid or resolved_cancelled:
        invalidate_stats_cache()

    _reconciliation_stats["last_run"] = now.isoformat()
    _reconciliation_stats["checked"] += checked
    _reconciliation_stats["resolved_paid"] += resolved_paid
    _reconciliation_stats["resolved_cancelled"] += resolved_cancelled
    _reconciliation_stats["errors"] += errors

    if checked:
        logger.info(
            f"Reconciliation: done — checked={checked} "
            f"paid={resolved_paid} cancelled={resolved_cancelled} errors={errors}"
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
            f"in-memory (per-process) — effective rate limits and cache TTLs will be N× higher. "
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
        _executor.shutdown(wait=True)

# ---------- FASTAPI APP ----------
app = FastAPI(title="Hot Portion Grill", version="1.0.0", lifespan=lifespan, docs_url="/docs")

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

@app.get("/health")
async def health():
    """
    Liveness + readiness. Reports 'degraded' if schema migrations failed —
    operators should wire this into their alerting so silent schema drift
    doesn't go unnoticed.
    """
    return {
        "status": "healthy" if _schema_state["ready"] else "degraded",
        "schema_ready": _schema_state["ready"],
        "schema_errors": _schema_state["errors"][:5],  # cap to avoid huge payloads
        "schema_last_run": _schema_state["last_run"],
        "reconciliation": {
            "enabled": settings.RECONCILIATION_ENABLED,
            "last_run": _reconciliation_stats["last_run"],
            "checked": _reconciliation_stats["checked"],
            "resolved_paid": _reconciliation_stats["resolved_paid"],
            "resolved_cancelled": _reconciliation_stats["resolved_cancelled"],
            "errors": _reconciliation_stats["errors"],
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

@app.get("/api/orders/by-reference/{ref}")
async def get_order_by_reference(ref: str, email: Optional[str] = Query(None)):
    if not ref or len(ref) > 128:
        raise HTTPException(400, "Invalid reference")
    db = get_supabase()
    query = db.table("orders").select("*").eq("payment_reference", ref)
    if email:
        query = query.eq("customer_email", email.strip().lower())
    r = await execute_db(query.limit(1))
    if r.data:
        return r.data[0]
    query = db.table("orders").select("*").eq("monnify_transaction_ref", ref)
    if email:
        query = query.eq("customer_email", email.strip().lower())
    r = await execute_db(query.limit(1))
    if r.data:
        return r.data[0]
    raise HTTPException(404, "Order not found")

@app.post("/api/orders", status_code=201)
async def create_order(order: OrderCreate, request: Request, bg: BackgroundTasks):
    await enforce_rate_limit(request, "orders", settings.RATE_LIMIT_ORDERS_PER_MINUTE)
    if not order.items:
        raise HTTPException(400, "Order must contain at least one item")
    product_ids = [item.product_id for item in order.items]
    if any(not pid for pid in product_ids):
        raise HTTPException(400, "All order items must have a product_id")
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
    computed_delivery_fee = 0
    if order.delivery_method == "delivery" and order.delivery_address:
        try:
            geocode_url = "https://nominatim.openstreetmap.org/search"
            params = {"q": order.delivery_address, "format": "json", "limit": 1}
            headers = {"User-Agent": "HotPortionGrill/1.0"}
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
                async with session.get(geocode_url, params=params, headers=headers) as resp:
                    if resp.status == 200:
                        geo_data = await resp.json()
                        if geo_data:
                            lat = float(geo_data[0].get("lat", 0))
                            lng = float(geo_data[0].get("lon", 0))
                            area_result = await execute_db(
                                get_supabase().rpc("find_delivery_area", {"lat": lat, "lng": lng})
                            )
                            if area_result.data:
                                base_fee = area_result.data[0].get("fee", 0)
                                calc = await calculate_intelligent_delivery_fee(
                                    base_fee=base_fee, order_value=computed_subtotal,
                                    items=order.items, order_time=datetime.now(),
                                    customer_email=order.customer_email,
                                )
                                computed_delivery_fee = calc["final_fee"]
                                logger.info(
                                    f"Server-recomputed delivery fee for '{order.delivery_address}': "
                                    f"₦{computed_delivery_fee} (base {base_fee})"
                                )
        except Exception as e:
            logger.warning(f"Server-side delivery fee computation failed, using client value: {e}")
            computed_delivery_fee = order.delivery_fee or 0
    server_payment_reference = f"HP-{secrets.token_urlsafe(16)}"
    computed_total = computed_subtotal + computed_delivery_fee
    data = {
        "payment_reference": server_payment_reference,
        "customer_name": order.customer_name,
        "customer_email": order.customer_email,
        "customer_phone": order.customer_phone,
        "total": computed_total,
        "status": "pending",
        "delivery_method": order.delivery_method or "pickup",
        "delivery_address": order.delivery_address,
        "preferred_time": order.preferred_time,
        "order_notes": order.order_notes,
        "items": validated_items,
        "delivery_fee": computed_delivery_fee,
        "delivery_breakdown": order.delivery_breakdown,
    }
    result = await execute_db(get_supabase().table("orders").insert(data))
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
        await execute_db(get_supabase().table("orders").delete().eq("id", order_data["id"]))
        raise HTTPException(400, f"Payment initialization failed: {monnify_result.get('error', 'Unknown error')}")
    await execute_db(
        get_supabase().table("orders")
        .update({"monnify_transaction_ref": monnify_result["transaction_reference"]})
        .eq("id", order_data["id"])
    )
    bg.add_task(get_brevo().send_order_confirmation, order_data)
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
        if request.lat is not None and request.lng is not None:
            lat = float(request.lat)
            lng = float(request.lng)
            logger.info(f"Using client-provided coords: lat={lat}, lng={lng} for '{request.address}'")
        else:
            geocode_url = "https://nominatim.openstreetmap.org/search"
            params = {"q": request.address, "format": "json", "limit": 1}
            headers = {"User-Agent": "HotPortionGrill/1.0"}
            async with aiohttp.ClientSession() as session:
                async with session.get(geocode_url, params=params, headers=headers) as resp:
                    if resp.status != 200:
                        logger.error(f"Geocoding API error: {resp.status}")
                        raise HTTPException(502, "Geocoding service temporarily unavailable")
                    data = await resp.json()
                    if not data or len(data) == 0:
                        return DeliveryFeeResponse(covered=False, message="Address not found. Please check the address and try again.")
                    lat = float(data[0].get("lat", 0))
                    lng = float(data[0].get("lon", 0))
                    logger.info(f"Geocoded '{request.address}' to lat={lat}, lng={lng}")
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

ALLOWED_PLACES_ENDPOINTS = {"autocomplete", "geocode", "reverse-geocode"}

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
        logger.info(f"Webhook duplicate for order {order['id']} (already paid) — ignoring")
        return {"status": "already_processed"}
    claim_result = await execute_db(
        get_supabase().table("orders")
        .update({"status": "paid"}).eq("id", order["id"]).neq("status", "paid")
    )
    if not claim_result.data:
        logger.info(f"Order {order['id']} already claimed by another worker — skipping stock decrement")
        return {"status": "already_processed"}
    await reduce_order_stock(order)
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
    """Invite a new staff member via Supabase Auth and create their staff record."""
    email = payload.email.strip().lower()
    role = payload.role.strip().lower()
    if role not in ALLOWED_ROLES:
        raise HTTPException(400, f"Invalid role. Must be one of: {sorted(ALLOWED_ROLES)}")
    if role == "owner" and staff.get("role") != "owner":
        raise HTTPException(403, "Only an owner can create another owner")

    existing = await execute_db(get_supabase().table("staff").select("id").eq("email", email))
    if existing.data:
        raise HTTPException(400, "A staff member with this email already exists")

    # Send the Supabase invite. Uses the admin REST endpoint with service_role key.
    invite_url = f"{settings.SUPABASE_URL}/auth/v1/invite"
    headers = {
        "apikey": settings.SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {settings.SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    # Where Supabase should redirect the user after verifying the invite token.
    # This URL MUST be whitelisted in Supabase Dashboard → Authentication → URL Configuration.
    invite_redirect = settings.INVITE_REDIRECT_URL
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        async with session.post(
            invite_url, headers=headers,
            json={
                "email": email,
                "data": {"full_name": payload.full_name, "role": role},
                "redirect_to": invite_redirect,
            }
        ) as resp:
            text = await resp.text()
            if resp.status not in (200, 201):
                logger.error(f"Supabase invite failed: {resp.status} {text[:200]}")
                raise HTTPException(502, f"Failed to send invite: {text[:200]}")
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                raise HTTPException(502, "Invalid response from Supabase invite")
            user_id = data.get("id")
            if not user_id:
                raise HTTPException(500, "Supabase invite did not return a user id")

    row = {
        "id": user_id,
        "email": email,
        "full_name": payload.full_name,
        "role": role,
        "is_active": True,
    }
    r = await execute_db(get_supabase().table("staff").insert(row))
    if not r.data:
        raise HTTPException(500, "Failed to create staff record")
    await audit_log(staff, "invite", "staff", user_id, {"email": email, "role": role}, request)
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

@app.get("/api/orders", response_model=List[Dict])
async def get_orders(
    since: Optional[str] = Query(None),
    staff: Dict[str, Any] = Depends(require_permission("orders:read")),
):
    query = get_supabase().table("orders").select("*").order("created_at", desc=True)
    if since:
        query = query.gt("created_at", since)
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
    if upd.status == "cancelled" and current_status not in ("cancelled",):
        await restore_order_stock(order)
        logger.info(f"Stock restored for cancelled order {oid}")
    if upd.status in ("confirmed", "paid") and current_status not in ("confirmed", "paid"):
        await reduce_order_stock(order)
    result = await execute_db(get_supabase().table("orders").update(update_data).eq("id", oid))
    if not result.data:
        raise HTTPException(404, "Order not found")
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
    await reduce_order_stock(order)
    await execute_db(get_supabase().table("orders").update({"status": "confirmed"}).eq("id", oid))
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
