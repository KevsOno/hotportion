import asyncio
import json
import os
import hmac
import hashlib
import base64
import logging
import time
import re
import contextvars
from datetime import datetime, timedelta, date
from typing import List, Optional, Dict, Any
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor

import aiohttp
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

load_dotenv()


# ---------- HELPER FUNCTION ----------
def convert_datetime_to_iso(obj):
    """
    Recursively convert all datetime and date objects in obj to ISO format strings.
    Handles dicts, lists, and any object with a __dict__ attribute.
    """
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
        "https://your-netlify-site.netlify.app",
        "http://localhost:3000",
        "http://localhost:8000"
    ]
    MAX_DB_THREADS: int = 25
    STATS_CACHE_TTL_SECONDS: int = 10
    DEBUG: bool = False

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
            "timestamp": datetime.utcnow().isoformat() + "Z",
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
    return await loop.run_in_executor(_executor, query.execute)

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
    # New fields for intelligent delivery
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
    product_id: int   # added for stock update

class OrderCreate(BaseModel):
    payment_reference: str
    customer_name: str
    customer_email: str
    customer_phone: str
    total: int
    status: Optional[str] = "pending"
    delivery_method: Optional[str] = "pickup"
    delivery_address: Optional[str] = None
    preferred_time: Optional[str] = None
    order_notes: Optional[str] = None
    items: List[OrderItem]
    monnify_transaction_ref: Optional[str] = None
    delivery_fee: Optional[int] = 0  # Added delivery_fee field

class OrderStatusUpdate(BaseModel):
    status: str

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

# =============================================
# DELIVERY AREA MODELS (Existing + Extended)
# =============================================

class DeliveryAreaBase(BaseModel):
    name: str
    fee: int

class DeliveryAreaCreate(DeliveryAreaBase):
    polygon: Dict[str, Any]  # GeoJSON Polygon

class DeliveryAreaUpdate(DeliveryAreaBase):
    polygon: Dict[str, Any]  # GeoJSON Polygon

class DeliveryArea(DeliveryAreaBase):
    id: int
    polygon: Dict[str, Any]  # GeoJSON representation
    created_at: datetime
    updated_at: datetime

# -------- New Intelligent Delivery Models --------
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
    day_of_week: Optional[int] = None  # 0=Sunday, 1=Monday...
    start_time: Optional[str] = None   # 'HH:MM'
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

class DeliveryFeeRequest(BaseModel):
    address: str
    items: List[OrderItem]  # Items with product_id and qty
    order_total: int
    order_time: Optional[datetime] = None
    customer_email: Optional[str] = None  # to check loyalty

class DeliveryFeeResponse(BaseModel):
    covered: bool
    base_fee: Optional[int] = None
    area_name: Optional[str] = None
    value_discount: int = 0
    item_surcharge: int = 0
    peak_surcharge: int = 0
    loyalty_discount: int = 0
    total_fee: int = 0
    breakdown: Optional[Dict[str, Any]] = None
    message: Optional[str] = None

# ---------- AI SERVICE ----------
logger = logging.getLogger(__name__)

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
        replacements = {
            '3': 'e', '4': 'a', '0': 'o',
            '@': 'a', '$': 's', '5': 's'
        }
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
                model=settings.GROQ_MODEL,
                messages=messages,
                temperature=0.3,
                max_tokens=700,
                timeout=10.0
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
                model=settings.OPENAI_MODEL,
                messages=messages,
                temperature=0.3,
                max_tokens=700,
                timeout=10.0
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
        self,
        amount: int,
        customer_name: str,
        customer_email: str,
        customer_phone: str,
        payment_reference: str,
        payment_description: str = "Hot Portion Grill Order"
    ) -> Dict[str, Any]:
        if not self.healthy:
            try:
                await self.initialize()
            except Exception as e:
                return {"success": False, "error": f"Monnify not healthy: {e}"}

        token = await self._get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

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
                json=payload,
                headers=headers
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

    async def handle_webhook(self, payload: dict, signature: str) -> Dict:
        if not self.healthy:
            return {"valid": False, "error": "Monnify not ready"}
        computed = hmac.new(self.secret_key.encode(), json.dumps(payload).encode(), hashlib.sha512).hexdigest()
        if computed != signature:
            return {"valid": False, "error": "Invalid signature"}
        return {"valid": True, "event": payload.get("eventType")}

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

# Caches for rules (TTL 60 seconds)
_rules_cache = {"data": None, "timestamp": 0}
_peak_cache = {"data": None, "timestamp": 0}
_loyalty_cache = {"data": None, "timestamp": 0}

async def get_delivery_fee_rules() -> List[Dict]:
    """Fetch active value‑discount rules from DB."""
    now = time.time()
    if now - _rules_cache["timestamp"] < 60 and _rules_cache["data"] is not None:
        return _rules_cache["data"]
    db = get_supabase()
    # Order by min_order_value ascending so we can apply the first matching rule
    result = await execute_db(
        db.table("delivery_fee_rules").select("*").order("min_order_value")
    )
    rules = result.data or []
    _rules_cache["data"] = rules
    _rules_cache["timestamp"] = now
    return rules

async def get_peak_settings() -> List[Dict]:
    """Fetch active peak hour settings."""
    now = time.time()
    if now - _peak_cache["timestamp"] < 60 and _peak_cache["data"] is not None:
        return _peak_cache["data"]
    db = get_supabase()
    result = await execute_db(
        db.table("delivery_peak_settings").select("*").eq("is_active", True)
    )
    settings_list = result.data or []
    _peak_cache["data"] = settings_list
    _peak_cache["timestamp"] = now
    return settings_list

async def get_loyalty_setting() -> Optional[Dict]:
    """Fetch the active loyalty discount setting."""
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

def is_peak_hour(order_time: datetime, peak_settings: List[Dict]) -> bool:
    """
    Check if the given order_time falls into any active peak period.
    If no order_time provided, use current time.
    """
    if not order_time:
        order_time = datetime.now()
    # Use local time without timezone – assume server time is Lagos time
    # If you have timezone info, convert accordingly.
    dow = order_time.weekday()  # Monday=0, Sunday=6
    hour_min = order_time.strftime("%H:%M")
    for setting in peak_settings:
        # If day_of_week is None, it applies to all days
        if setting.get("day_of_week") is not None and setting["day_of_week"] != dow:
            continue
        start = setting.get("start_time")
        end = setting.get("end_time")
        if start and end:
            if start <= hour_min <= end:
                return True
    return False

async def calculate_intelligent_delivery_fee(
    base_fee: int,
    order_value: int,
    items: List[OrderItem],
    order_time: Optional[datetime] = None,
    customer_email: Optional[str] = None
) -> Dict[str, Any]:
    """
    Calculate the final delivery fee with all modifiers.
    Returns a dict with breakdown and final fee.
    """
    fee = base_fee
    value_discount = 0
    item_surcharge = 0
    peak_surcharge = 0
    loyalty_discount = 0

    # 1. Value Discount (based on order total)
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
                # Apply multiplier first, then subtract fixed discount
                new_fee = fee * multiplier - discount_amount
                value_discount = fee - new_fee
                fee = max(0, new_fee)
            break  # apply first matching rule

    # 2. Item Surcharge (bulk / weight)
    if items:
        # Fetch product details to get is_main_item, weight_kg, is_bulky
        product_ids = [item.product_id for item in items]
        db = get_supabase()
        # We need to get product fields: is_main_item, weight_kg, is_bulky
        # Use a select with in_ clause
        result = await execute_db(
            db.table("products").select("id, is_main_item, weight_kg, is_bulky").in_("id", product_ids)
        )
        product_map = {p["id"]: p for p in result.data} if result.data else {}

        main_count = 0
        total_weight = 0.0
        for item in items:
            pid = item.product_id
            prod = product_map.get(pid)
            if prod:
                if prod.get("is_main_item", True):
                    main_count += item.qty
                weight = prod.get("weight_kg", 0.5)
                total_weight += weight * item.qty
            else:
                # fallback: treat as main item with default weight
                main_count += item.qty
                total_weight += 0.5 * item.qty

        # Surcharge based on main item count
        if main_count > 10:
            item_surcharge += 500
        elif main_count > 6:
            item_surcharge += 300

        # Weight-based surcharge
        if total_weight > 5.0:
            extra_kg = total_weight - 5.0
            item_surcharge += int(extra_kg * 50)  # ₦50 per kg over 5kg

    fee += item_surcharge

    # 3. Peak Time Surcharge
    peak_settings = await get_peak_settings()
    if is_peak_hour(order_time or datetime.now(), peak_settings):
        peak_surcharge = 200  # Could also fetch from settings
        if fee > 0:
            fee += peak_surcharge

    # 4. Loyalty Discount
    # Check if customer has placed enough orders (if email provided)
    if customer_email:
        loyalty_setting = await get_loyalty_setting()
        if loyalty_setting:
            min_orders = loyalty_setting.get("min_orders", 5)
            discount_pct = loyalty_setting.get("discount_percentage", 20)
            # Count orders for this customer with status in ('paid','confirmed','completed')
            db = get_supabase()
            count_result = await execute_db(
                db.table("orders")
                .select("id", count="exact")
                .eq("customer_email", customer_email)
                .in_("status", ["paid", "confirmed", "completed"])
            )
            order_count = count_result.count or 0
            if order_count >= min_orders and fee > 0:
                loyalty_discount = int(fee * discount_pct / 100)
                fee = max(0, fee - loyalty_discount)

    final_fee = max(0, round(fee))

    return {
        "base_fee": base_fee,
        "value_discount": value_discount,
        "item_surcharge": item_surcharge,
        "peak_surcharge": peak_surcharge,
        "loyalty_discount": loyalty_discount,
        "final_fee": final_fee,
        "breakdown": {
            "base": base_fee,
            "value_discount": -value_discount,
            "item_surcharge": item_surcharge,
            "peak_surcharge": peak_surcharge,
            "loyalty_discount": -loyalty_discount,
            "final": final_fee
        }
    }

# ---------- DATABASE SETUP ----------
async def setup_database():
    db = get_supabase()
    try:
        # Add columns to products if they don't exist (for safety)
        # We'll use raw SQL via RPC if available, else log warning.
        alter_queries = [
            "ALTER TABLE products ADD COLUMN IF NOT EXISTS is_main_item BOOLEAN DEFAULT TRUE;",
            "ALTER TABLE products ADD COLUMN IF NOT EXISTS weight_kg DECIMAL(4,2) DEFAULT 0.5;",
            "ALTER TABLE products ADD COLUMN IF NOT EXISTS is_bulky BOOLEAN DEFAULT FALSE;",
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivery_fee INTEGER DEFAULT 0;"
        ]
        for q in alter_queries:
            try:
                await execute_db(db.rpc("exec_sql", {"query": q}))
            except Exception as e:
                logger.warning(f"Could not run alter (may already exist): {e}")

        # Create new tables if they don't exist
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
                day_of_week INTEGER,  -- 0=Sunday, 1=Monday, etc.
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
            """
        ]
        for sql in create_tables:
            try:
                await execute_db(db.rpc("exec_sql", {"query": sql}))
            except Exception as e:
                logger.warning(f"Could not create table: {e}")

        logger.info("Database indexes, columns, and tables ensured.")
    except Exception as e:
        logger.warning(f"Could not create tables/columns (RPC may be disabled): {e}")

# ---------- LIFESPAN ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Hot Portion Grill - Full Monolith + AI")
    init_results = await asyncio.gather(
        get_brevo().initialize(),
        get_monnify().initialize(),
        return_exceptions=True
    )
    for i, result in enumerate(init_results):
        if isinstance(result, Exception):
            logger.error(f"Init service {i} failed: {result}")

    asyncio.create_task(setup_database())
    logger.info("All services ready (degraded mode allowed for external APIs)")
    yield
    logger.info("Shutting down...")
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

        # Filter by date range in Python
        filtered_data = []
        for banner in result.data:
            start = banner.get('start_date')
            end = banner.get('end_date')
            if start and start > now:
                continue
            if end and end < now:
                continue
            filtered_data.append(banner)

        # Enrich with categories and products
        for b in filtered_data:
            b['categories'] = await self._get_categories(b['id'])
            b['products'] = await self._get_products(b['id'])

        # ─── CONVERT ALL DATETIME OBJECTS TO ISO STRINGS ───
        filtered_data = [convert_datetime_to_iso(b) for b in filtered_data]

        active_count = len([x for x in filtered_data if x.get('is_active', True)])
        hero_count = len([x for x in filtered_data if x.get('is_hero', False)])
        featured_count = len([x for x in filtered_data if x.get('is_featured', False)])

        return BannerResponse(
            banners=filtered_data,
            total=len(filtered_data),
            active_count=active_count,
            hero_count=hero_count,
            featured_count=featured_count
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

        # Enrich with categories and products
        for b in filtered_data:
            b['categories'] = await self._get_categories(b['id'])
            b['products'] = await self._get_products(b['id'])

        # ─── CONVERT ALL DATETIME OBJECTS TO ISO STRINGS ───
        filtered_data = [convert_datetime_to_iso(b) for b in filtered_data]

        return filtered_data

    async def get_banner(self, banner_id: int):
        result = await execute_db(self.db.table(self.table).select("*").eq("id", banner_id))
        if not result.data:
            return None
        b = result.data[0]
        b['categories'] = await self._get_categories(b['id'])
        b['products'] = await self._get_products(b['id'])

        # ─── CONVERT ALL DATETIME OBJECTS TO ISO STRINGS ───
        b = convert_datetime_to_iso(b)

        return b

    async def create_banner(self, banner: BannerCreate):
        data = banner.dict(exclude={'categories', 'products'})
        data["created_at"] = data["updated_at"] = datetime.now().isoformat()

        # ─── CONVERT ALL DATETIME OBJECTS TO ISO STRINGS ───
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

        # ─── CONVERT ALL DATETIME OBJECTS TO ISO STRINGS ───
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

# ---------- API ROUTES ----------
@app.get("/health")
async def health():
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}

@app.get("/api/products", response_model=List[Product])
async def get_products():
    r = await execute_db(get_supabase().table("products").select("*").order("name"))
    return r.data

@app.post("/api/products", response_model=Product, status_code=201)
async def create_product(p: ProductCreate):
    r = await execute_db(get_supabase().table("products").insert(p.dict()))
    if not r.data:
        raise HTTPException(400, "Failed")
    return r.data[0]

@app.put("/api/products/{pid}")
async def update_product(pid: int, p: ProductUpdate):
    r = await execute_db(get_supabase().table("products").update(p.dict()).eq("id", pid))
    if not r.data:
        raise HTTPException(404, "Not found")
    return r.data[0]

@app.delete("/api/products/{pid}", status_code=204)
async def delete_product(pid: int):
    r = await execute_db(get_supabase().table("products").delete().eq("id", pid))
    if not r.data:
        raise HTTPException(404, "Not found")

@app.get("/api/categories", response_model=List[Category])
async def get_categories():
    r = await execute_db(get_supabase().table("categories").select("*").order("name"))
    return r.data

@app.get("/api/top-products")
async def get_top_products(limit: int = 20):
    """
    Returns the top N products by total quantity sold (from paid/confirmed/completed orders).
    Also includes total revenue for each product.
    """
    # Fetch all completed orders (status: paid, confirmed, completed)
    query = get_supabase().table("orders").select("items").in_("status", ["paid", "confirmed", "completed"])
    r = await execute_db(query)
    orders = r.data

    # Aggregate per product
    totals = {}  # product_id -> {'qty': total, 'revenue': total}
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

    # Sort by quantity descending and take top N
    sorted_items = sorted(totals.items(), key=lambda x: x[1]['qty'], reverse=True)[:limit]
    product_ids = [pid for pid, _ in sorted_items]

    # Fetch product names, emojis
    if product_ids:
        prod_res = await execute_db(
            get_supabase().table("products")
            .select("id, name, price, emoji")
            .in_("id", product_ids)
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


@app.post("/api/categories", response_model=Category, status_code=201)
async def create_category(c: CategoryBase):
    r = await execute_db(get_supabase().table("categories").insert(c.dict()))
    if not r.data:
        raise HTTPException(400, "Failed")
    return r.data[0]

@app.put("/api/categories/{cid}")
async def update_category(cid: int, c: CategoryBase):
    r = await execute_db(get_supabase().table("categories").update(c.dict()).eq("id", cid))
    if not r.data:
        raise HTTPException(404, "Not found")
    return r.data[0]

@app.delete("/api/categories/{cid}", status_code=204)
async def delete_category(cid: int):
    r = await execute_db(get_supabase().table("categories").delete().eq("id", cid))
    if not r.data:
        raise HTTPException(404, "Not found")

@app.get("/api/orders", response_model=List[Dict])
async def get_orders(since: Optional[str] = Query(None)):
    """
    Get all orders. If 'since' is provided (ISO timestamp), only return orders
    created after that time.
    """
    query = get_supabase().table("orders").select("*").order("created_at", desc=True)
    
    if since:
        # Filter by created_at > since (ISO format works directly with Postgres timestamptz)
        query = query.gt("created_at", since)
    
    r = await execute_db(query)
    
    # Add itemCount for each order
    for o in r.data:
        o["itemCount"] = len(o.get("items", []))
    
    return r.data

@app.get("/api/orders/{oid}")
async def get_order(oid: int):
    r = await execute_db(get_supabase().table("orders").select("*").eq("id", oid))
    if not r.data:
        raise HTTPException(404, "Not found")
    return r.data[0]

@app.post("/api/orders", status_code=201)
async def create_order(order: OrderCreate, bg: BackgroundTasks):
    """Create a new order with optional delivery_fee."""
    data = order.dict(exclude={'monnify_transaction_ref'})
    
    # Ensure delivery_fee is included (defaults to 0 from model)
    if "delivery_fee" not in data:
        data["delivery_fee"] = 0
    
    result = await execute_db(get_supabase().table("orders").insert(data))
    if not result.data:
        raise HTTPException(400, "Failed to create order")

    order_data = result.data[0]

    monnify = get_monnify()
    monnify_result = await monnify.initialize_transaction(
        amount=order.total,
        customer_name=order.customer_name,
        customer_email=order.customer_email,
        customer_phone=order.customer_phone,
        payment_reference=order.payment_reference,
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
        "payment_reference": order.payment_reference,
        "checkout_url": monnify_result["checkout_url"]
    }

@app.patch("/api/orders/{oid}/status")
async def update_order_status(oid: int, upd: OrderStatusUpdate):
    r = await execute_db(get_supabase().table("orders").update({"status": upd.status}).eq("id", oid))
    if not r.data:
        raise HTTPException(404, "Not found")
    return r.data[0]

@app.get("/api/stats")
async def get_stats():
    return await get_cached_stats()

@app.get("/api/v1/banners", response_model=BannerResponse)
async def list_banners(
    is_active: Optional[bool] = Query(None), is_hero: Optional[bool] = Query(None),
    is_featured: Optional[bool] = Query(None), category: Optional[str] = Query(None),
    limit: int = 50, offset: int = 0
):
    return await BannerService().get_banners(is_active, is_hero, is_featured, category, limit, offset)

@app.get("/api/v1/banners/active", response_model=List[Banner])
async def list_active_banners(is_hero: Optional[bool] = Query(None), is_featured: Optional[bool] = Query(None)):
    return await BannerService().get_active(is_hero, is_featured)

@app.get("/api/v1/banners/{banner_id}", response_model=Banner)
async def get_banner(banner_id: int):
    b = await BannerService().get_banner(banner_id)
    if not b:
        raise HTTPException(404, "Not found")
    return b

@app.post("/api/v1/banners", response_model=Banner, status_code=201)
async def create_banner(banner: BannerCreate):
    return await BannerService().create_banner(banner)

@app.put("/api/v1/banners/{banner_id}", response_model=Banner)
async def update_banner(banner_id: int, banner: BannerUpdate):
    return await BannerService().update_banner(banner_id, banner)

@app.delete("/api/v1/banners/{banner_id}", status_code=204)
async def delete_banner(banner_id: int):
    await BannerService().delete_banner(banner_id)

@app.patch("/api/v1/banners/{banner_id}/toggle")
async def toggle_banner(banner_id: int):
    return await BannerService().toggle_banner(banner_id)

@app.post("/api/v1/banners/{banner_id}/duplicate")
async def duplicate_banner(banner_id: int):
    return await BannerService().duplicate_banner(banner_id)

@app.patch("/api/v1/banners/reorder")
async def reorder_banners(banner_ids: List[int]):
    return await BannerService().reorder_banners(banner_ids)

@app.post("/api/v1/webhooks/monnify")
async def monnify_webhook(
    payload: dict,
    x_signature: Optional[str] = Header(None, alias="X-Signature")
):
    result = await get_monnify().handle_webhook(payload, x_signature)
    if not result["valid"]:
        raise HTTPException(400, "Invalid signature")

    if result.get("event") == "SUCCESSFUL_TRANSACTION":
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

        # Reduce stock for each item
        items = order.get("items", [])
        for item in items:
            product_id = item.get("product_id")
            qty = item.get("qty", 0)
            if product_id and qty > 0:
                prod_result = await execute_db(
                    get_supabase().table("products").select("stock").eq("id", product_id)
                )
                if prod_result.data:
                    current_stock = prod_result.data[0].get("stock", 0)
                    new_stock = max(0, current_stock - qty)
                    await execute_db(
                        get_supabase().table("products")
                        .update({"stock": new_stock})
                        .eq("id", product_id)
                    )
                    logger.info(f"Stock updated for product {product_id}: {current_stock} → {new_stock}")

        invalidate_stats_cache()

        if order.get("status") != "paid":
            await execute_db(
                get_supabase().table("orders")
                .update({"status": "paid"})
                .eq("id", order["id"])
            )

    return {"status": "received"}

# =============================================
# DELIVERY AREA ENDPOINTS (UPDATED WITH INTELLIGENCE)
# =============================================

@app.post("/api/delivery-fee", response_model=DeliveryFeeResponse)
async def get_delivery_fee(request: DeliveryFeeRequest):
    """
    Calculate the intelligent delivery fee for a given address and order details.
    """
    try:
        # Geocode address using Nominatim
        geocode_url = "https://nominatim.openstreetmap.org/search"
        params = {
            "q": request.address,
            "format": "json",
            "limit": 1
        }
        headers = {
            "User-Agent": "HotPortionGrill/1.0"
        }

        async with aiohttp.ClientSession() as session:
            async with session.get(geocode_url, params=params, headers=headers) as resp:
                if resp.status != 200:
                    logger.error(f"Geocoding API error: {resp.status}")
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail="Geocoding service temporarily unavailable"
                    )
                
                data = await resp.json()
                
                if not data or len(data) == 0:
                    return DeliveryFeeResponse(
                        covered=False,
                        message="Address not found. Please check the address and try again."
                    )
                
                lat = float(data[0].get("lat", 0))
                lng = float(data[0].get("lon", 0))
                
                logger.info(f"Geocoded '{request.address}' to lat={lat}, lng={lng}")
                
                # Find delivery area using Supabase RPC
                db = get_supabase()
                result = await execute_db(
                    db.rpc("find_delivery_area", {"lat": lat, "lng": lng})
                )
                
                if not result.data or len(result.data) == 0:
                    return DeliveryFeeResponse(
                        covered=False,
                        message="Address not in any delivery area."
                    )
                
                area = result.data[0]
                base_fee = area.get("fee", 0)
                area_name = area.get("name", "Unknown Area")

                # Now calculate intelligent fee
                order_time = request.order_time or datetime.now()
                calculation = await calculate_intelligent_delivery_fee(
                    base_fee=base_fee,
                    order_value=request.order_total,
                    items=request.items,
                    order_time=order_time,
                    customer_email=request.customer_email
                )

                return DeliveryFeeResponse(
                    covered=True,
                    base_fee=base_fee,
                    area_name=area_name,
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
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error processing delivery fee request"
        )


@app.get("/api/delivery-areas", response_model=List[DeliveryArea])
async def get_all_delivery_areas():
    """
    Get all delivery areas with polygons as GeoJSON.
    """
    try:
        db = get_supabase()
        result = await execute_db(
            db.rpc("get_delivery_areas_geojson", {})
        )
        
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
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error fetching delivery areas"
        )


@app.post("/api/delivery-areas", response_model=DeliveryArea, status_code=201)
async def create_delivery_area(area: DeliveryAreaCreate):
    """
    Create a new delivery area.
    """
    try:
        if "type" not in area.polygon or area.polygon["type"] != "Polygon":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid polygon: must be a GeoJSON Polygon"
            )
        
        if "coordinates" not in area.polygon or not area.polygon["coordinates"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid polygon: missing coordinates"
            )
        
        coords = area.polygon["coordinates"][0]
        if len(coords) < 4:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid polygon: must have at least 4 points"
            )
        
        db = get_supabase()
        result = await execute_db(
            db.rpc(
                "insert_delivery_area",
                {
                    "_name": area.name,
                    "_fee": area.fee,
                    "_geojson": json.dumps(area.polygon)
                }
            )
        )
        
        if not result.data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to create delivery area. Check that the polygon is valid."
            )
        
        created = dict(result.data[0])
        created["polygon"] = area.polygon
        
        if "created_at" in created and isinstance(created["created_at"], (datetime, date)):
            created["created_at"] = created["created_at"].isoformat()
        if "updated_at" in created and isinstance(created["updated_at"], (datetime, date)):
            created["updated_at"] = created["updated_at"].isoformat()
        
        return created
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating delivery area: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error creating delivery area: {str(e)}"
        )


@app.put("/api/delivery-areas/{area_id}", response_model=DeliveryArea)
async def update_delivery_area(area_id: int, area: DeliveryAreaUpdate):
    """
    Update an existing delivery area.
    """
    try:
        if "type" not in area.polygon or area.polygon["type"] != "Polygon":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid polygon: must be a GeoJSON Polygon"
            )
        
        if "coordinates" not in area.polygon or not area.polygon["coordinates"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid polygon: missing coordinates"
            )
        
        coords = area.polygon["coordinates"][0]
        if len(coords) < 4:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid polygon: must have at least 4 points"
            )
        
        db = get_supabase()
        result = await execute_db(
            db.rpc(
                "update_delivery_area",
                {
                    "_id": area_id,
                    "_name": area.name,
                    "_fee": area.fee,
                    "_geojson": json.dumps(area.polygon)
                }
            )
        )
        
        if not result.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Delivery area with ID {area_id} not found"
            )
        
        updated = dict(result.data[0])
        updated["polygon"] = area.polygon
        
        if "created_at" in updated and isinstance(updated["created_at"], (datetime, date)):
            updated["created_at"] = updated["created_at"].isoformat()
        if "updated_at" in updated and isinstance(updated["updated_at"], (datetime, date)):
            updated["updated_at"] = updated["updated_at"].isoformat()
        
        return updated
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating delivery area: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error updating delivery area: {str(e)}"
        )


@app.delete("/api/delivery-areas/{area_id}", status_code=204)
async def delete_delivery_area(area_id: int):
    try:
        db = get_supabase()
        check_result = await execute_db(
            db.table("delivery_areas").select("id").eq("id", area_id)
        )
        if not check_result.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Delivery area with ID {area_id} not found"
            )
        
        await execute_db(
            db.table("delivery_areas").delete().eq("id", area_id)
        )
        
        return None
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting delivery area: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error deleting delivery area: {str(e)}"
        )


@app.get("/api/delivery-areas/point")
async def get_area_by_point(lat: float, lng: float):
    try:
        db = get_supabase()
        result = await execute_db(
            db.rpc("find_delivery_area", {"lat": lat, "lng": lng})
        )
        
        if result.data and len(result.data) > 0:
            return {
                "covered": True,
                "area": result.data[0]
            }
        else:
            return {
                "covered": False,
                "message": "Point not in any delivery area"
            }
            
    except Exception as e:
        logger.error(f"Error checking point: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error checking delivery area coverage"
        )

# =============================================
# ADMIN ENDPOINTS FOR DELIVERY RULES & SETTINGS
# =============================================

@app.get("/api/admin/delivery-rules", response_model=List[DeliveryFeeRule])
async def get_delivery_rules():
    db = get_supabase()
    result = await execute_db(db.table("delivery_fee_rules").select("*").order("min_order_value"))
    return result.data

@app.post("/api/admin/delivery-rules", response_model=DeliveryFeeRule, status_code=201)
async def create_delivery_rule(rule: DeliveryFeeRuleCreate):
    db = get_supabase()
    data = rule.dict(exclude={'id', 'created_at', 'updated_at'})
    data["created_at"] = data["updated_at"] = datetime.now().isoformat()
    result = await execute_db(db.table("delivery_fee_rules").insert(data))
    if not result.data:
        raise HTTPException(400, "Failed to create rule")
    return result.data[0]

@app.put("/api/admin/delivery-rules/{rule_id}", response_model=DeliveryFeeRule)
async def update_delivery_rule(rule_id: int, rule: DeliveryFeeRuleUpdate):
    db = get_supabase()
    data = rule.dict(exclude={'id', 'created_at', 'updated_at'}, exclude_unset=True)
    data["updated_at"] = datetime.now().isoformat()
    result = await execute_db(db.table("delivery_fee_rules").update(data).eq("id", rule_id))
    if not result.data:
        raise HTTPException(404, "Rule not found")
    return result.data[0]

@app.delete("/api/admin/delivery-rules/{rule_id}", status_code=204)
async def delete_delivery_rule(rule_id: int):
    db = get_supabase()
    result = await execute_db(db.table("delivery_fee_rules").delete().eq("id", rule_id))
    if not result.data:
        raise HTTPException(404, "Rule not found")

@app.get("/api/admin/peak-settings", response_model=List[DeliveryPeakSetting])
async def get_peak_settings():
    db = get_supabase()
    result = await execute_db(db.table("delivery_peak_settings").select("*").order("day_of_week", nulls_last=True))
    return result.data

@app.post("/api/admin/peak-settings", response_model=DeliveryPeakSetting, status_code=201)
async def create_peak_setting(setting: DeliveryPeakSettingCreate):
    db = get_supabase()
    data = setting.dict(exclude={'id', 'created_at', 'updated_at'})
    data["created_at"] = data["updated_at"] = datetime.now().isoformat()
    result = await execute_db(db.table("delivery_peak_settings").insert(data))
    if not result.data:
        raise HTTPException(400, "Failed to create peak setting")
    return result.data[0]

@app.put("/api/admin/peak-settings/{setting_id}", response_model=DeliveryPeakSetting)
async def update_peak_setting(setting_id: int, setting: DeliveryPeakSettingUpdate):
    db = get_supabase()
    data = setting.dict(exclude={'id', 'created_at', 'updated_at'}, exclude_unset=True)
    data["updated_at"] = datetime.now().isoformat()
    result = await execute_db(db.table("delivery_peak_settings").update(data).eq("id", setting_id))
    if not result.data:
        raise HTTPException(404, "Peak setting not found")
    return result.data[0]

@app.delete("/api/admin/peak-settings/{setting_id}", status_code=204)
async def delete_peak_setting(setting_id: int):
    db = get_supabase()
    result = await execute_db(db.table("delivery_peak_settings").delete().eq("id", setting_id))
    if not result.data:
        raise HTTPException(404, "Peak setting not found")

@app.get("/api/admin/loyalty-settings", response_model=List[DeliveryLoyaltySetting])
async def get_loyalty_settings():
    db = get_supabase()
    result = await execute_db(db.table("delivery_loyalty_settings").select("*"))
    return result.data

@app.post("/api/admin/loyalty-settings", response_model=DeliveryLoyaltySetting, status_code=201)
async def create_loyalty_setting(setting: DeliveryLoyaltySettingCreate):
    db = get_supabase()
    data = setting.dict(exclude={'id', 'created_at', 'updated_at'})
    data["created_at"] = data["updated_at"] = datetime.now().isoformat()
    result = await execute_db(db.table("delivery_loyalty_settings").insert(data))
    if not result.data:
        raise HTTPException(400, "Failed to create loyalty setting")
    return result.data[0]

@app.put("/api/admin/loyalty-settings/{setting_id}", response_model=DeliveryLoyaltySetting)
async def update_loyalty_setting(setting_id: int, setting: DeliveryLoyaltySettingUpdate):
    db = get_supabase()
    data = setting.dict(exclude={'id', 'created_at', 'updated_at'}, exclude_unset=True)
    data["updated_at"] = datetime.now().isoformat()
    result = await execute_db(db.table("delivery_loyalty_settings").update(data).eq("id", setting_id))
    if not result.data:
        raise HTTPException(404, "Loyalty setting not found")
    return result.data[0]

@app.delete("/api/admin/loyalty-settings/{setting_id}", status_code=204)
async def delete_loyalty_setting(setting_id: int):
    db = get_supabase()
    result = await execute_db(db.table("delivery_loyalty_settings").delete().eq("id", setting_id))
    if not result.data:
        raise HTTPException(404, "Loyalty setting not found")

# ---------- AI ENDPOINTS ----------
@app.post("/api/ai/chat", response_model=AIChatResponse)
async def chat_with_ai(req: AIChatRequest, ai_service: AIService = Depends(get_ai_service)):
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

# ---------- RUN ----------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, workers=1, log_level="info")
