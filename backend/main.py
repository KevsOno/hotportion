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
from datetime import datetime, timedelta
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

class ProductCreate(ProductBase): pass
class ProductUpdate(ProductBase): pass
class Product(ProductBase):
    id: int

class CategoryBase(BaseModel):
    name: str
class Category(CategoryBase):
    id: int

# UPDATED: OrderItem now includes product_id
class OrderItem(BaseModel):
    name: str
    qty: int
    price: int
    product_id: int   # <-- added

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
    # optional field to store Monnify transaction ref (updated after payment init)
    monnify_transaction_ref: Optional[str] = None

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

# ---------- AI SERVICE ----------


# Ensure groq, genai, openai, settings are imported in your module as before
logger = logging.getLogger(__name__)


class AIService:
    def __init__(self):
        self.groq_client = None
        self.genai_client = None
        self.openai_client = None
        self.supabase_client: Optional[Client] = None

        # Simple memory cache for Supabase database context
        self._context_cache: Optional[str] = None
        self._cache_timestamp: float = 0
        self._cache_ttl: int = 3000  # Cache context for 5 minutes (300 seconds)

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
            "Keep responses concise, friendly, and professional."
            # NEW INSTRUCTION:
            "When a user asks for the 'menu', 'what do you have', or 'list all items', "
            "respond with a clear list of all available products from the PRODUCTS section, "
            "including name and price. If the list is long, provide a summary and offer to give more details."
            # --- NEW FORMATTING INSTRUCTIONS ---
            "When providing information, format your response for readability:\n"
            "- Use bullet points (hyphens) for lists.\n"
            "- Put **item names** or **headings** in bold using asterisks (e.g., **Jollof Rice**).\n"
            "- Display prices as ₦X,XXX.\n"
            "- Use emojis sparingly to add visual cues (e.g., 🍚 for rice, 📍 for location, ⏰ for hours).\n"
            "- For menus, group items by category (e.g., Rice Dishes, Swallows) if possible.\n"
            "- For services, use a clear structure with short headings (e.g., **Delivery** – ...).\n"
            "- Keep lines short and use blank lines between sections for readability.\n"
            "When a user asks for the 'menu', 'what do you have', or 'list all items', "
            "respond with a clear list of all available products from the PRODUCTS section, "
            "including name and price. If the list is long, provide a summary and offer to give more details."
)

    def _init_clients(self):
        # Initialize Supabase Client
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

        # Initialize Groq Client
        if getattr(settings, 'GROQ_API_KEY', None) and groq is not None:
            self.groq_client = groq.Groq(api_key=settings.GROQ_API_KEY)
            logger.info("Groq client initialized")
        else:
            logger.warning("Groq client not available")

        # Initialize Gemini Client
        if getattr(settings, 'GEMINI_API_KEY', None) and genai is not None:
            genai.configure(api_key=settings.GEMINI_API_KEY)
            self.genai_client = genai.GenerativeModel(settings.GEMINI_MODEL)
            logger.info("Gemini client initialized")
        else:
            logger.warning("Gemini client not available")

        # Initialize OpenAI Client
        if getattr(settings, 'OPENAI_API_KEY', None) and openai is not None:
            self.openai_client = openai.OpenAI(api_key=settings.OPENAI_API_KEY)
            logger.info("OpenAI client initialized")
        else:
            logger.warning("OpenAI client not available")

    def _get_context(self) -> str:
        """Fetch and format context from Supabase with 5-minute memory caching."""
        current_time = time.time()
        
        # Return cached context if still valid
        if self._context_cache and (current_time - self._cache_timestamp < self._cache_ttl):
            return self._context_cache

        if not self.supabase_client:
            logger.warning("Supabase client unavailable, skipping database context fetch.")
            return ""

        try:
            # 1. Fetch Products
            products_res = self.supabase_client.table("products").select("*").execute()
            products = products_res.data if products_res.data else []

            # 2. Fetch Knowledge Base
            knowledge_res = self.supabase_client.table("knowledge").select("*").execute()
            knowledge = knowledge_res.data if knowledge_res.data else []

            # Format database content
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

            # Save to memory cache
            self._context_cache = context
            self._cache_timestamp = current_time
            logger.info("Supabase menu & knowledge base context refreshed")
            return context

        except Exception as e:
            logger.error(f"Error fetching Supabase context: {e}")
            # Fall back to expired cache if available to prevent outage
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

        # Run non-blocking in executor
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

        # Run non-blocking in executor
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

        # Truncate history to last 6 messages (3 turns)
        conversation_history = history[-6:] if history else []

        # Build dynamic system prompt with cached Supabase RAG data
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

# ---------- MONNIFY (Extended) ----------
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
        """
        Calls Monnify's Initialize Transaction endpoint.
        Returns:
            {
                "success": bool,
                "transaction_reference": str,
                "checkout_url": str,
                "error": str (if failed)
            }
        """
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

# ---------- DATABASE SETUP ----------
async def setup_database():
    db = get_supabase()
    try:
        queries = [
            "CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at DESC);",
            "CREATE INDEX IF NOT EXISTS idx_banners_active_display ON banners(is_active, display_order);",
            "CREATE INDEX IF NOT EXISTS idx_banners_dates ON banners(start_date, end_date);"
        ]
        for q in queries:
            await execute_db(db.rpc("exec_sql", {"query": q}))
        logger.info("Database indexes ensured.")
    except Exception as e:
        logger.warning(f"Could not create indexes (RPC may be disabled): {e}")

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
        if is_active is not None: query = query.eq("is_active", is_active)
        if is_hero is not None: query = query.eq("is_hero", is_hero)
        if is_featured is not None: query = query.eq("is_featured", is_featured)
        now = datetime.now().isoformat()
        query = query.or_(f"start_date.is.null,start_date.lte.{now}").or_(f"end_date.is.null,end_date.gte.{now}")
        count_res = await execute_db(query)
        total = count_res.count
        result = await execute_db(query.order("display_order").range(offset, offset + limit - 1))
        for b in result.data:
            b['categories'] = await self._get_categories(b['id'])
            b['products'] = await self._get_products(b['id'])
        return BannerResponse(
            banners=result.data, total=total,
            active_count=len([x for x in result.data if x.get('is_active')]),
            hero_count=len([x for x in result.data if x.get('is_hero')]),
            featured_count=len([x for x in result.data if x.get('is_featured')])
        )

    async def get_active(self, is_hero=None, is_featured=None):
        query = self.db.table(self.table).select("*").eq("is_active", True)
        now = datetime.now().isoformat()
        query = query.or_(f"start_date.is.null,start_date.lte.{now}").or_(f"end_date.is.null,end_date.gte.{now}")
        if is_hero is not None: query = query.eq("is_hero", is_hero)
        if is_featured is not None: query = query.eq("is_featured", is_featured)
        result = await execute_db(query.order("display_order"))
        for b in result.data:
            b['categories'] = await self._get_categories(b['id'])
            b['products'] = await self._get_products(b['id'])
        return result.data

    async def get_banner(self, banner_id: int):
        result = await execute_db(self.db.table(self.table).select("*").eq("id", banner_id))
        if not result.data: return None
        b = result.data[0]
        b['categories'] = await self._get_categories(b['id'])
        b['products'] = await self._get_products(b['id'])
        return b

    async def create_banner(self, banner: BannerCreate):
        data = banner.dict(exclude={'categories', 'products'})
        data["created_at"] = data["updated_at"] = datetime.now().isoformat()
        result = await execute_db(self.db.table(self.table).insert(data))
        if not result.data: raise HTTPException(400, "Create failed")
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
        result = await execute_db(self.db.table(self.table).update(data).eq("id", banner_id))
        if not result.data: raise HTTPException(404, "Not found")
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
        if not result.data: raise HTTPException(404, "Not found")

    async def toggle_banner(self, banner_id: int):
        b = await self.get_banner(banner_id)
        if not b: raise HTTPException(404, "Not found")
        new_status = not b['is_active']
        await execute_db(self.db.table(self.table).update({"is_active": new_status, "updated_at": datetime.now().isoformat()}).eq("id", banner_id))
        return {"id": banner_id, "is_active": new_status}

    async def duplicate_banner(self, banner_id: int):
        original = await self.get_banner(banner_id)
        if not original: raise HTTPException(404, "Not found")
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

# Products
@app.get("/api/products", response_model=List[Product])
async def get_products():
    r = await execute_db(get_supabase().table("products").select("*").order("name"))
    return r.data

@app.post("/api/products", response_model=Product, status_code=201)
async def create_product(p: ProductCreate):
    r = await execute_db(get_supabase().table("products").insert(p.dict()))
    if not r.data: raise HTTPException(400, "Failed")
    return r.data[0]

@app.put("/api/products/{pid}")
async def update_product(pid: int, p: ProductUpdate):
    r = await execute_db(get_supabase().table("products").update(p.dict()).eq("id", pid))
    if not r.data: raise HTTPException(404, "Not found")
    return r.data[0]

@app.delete("/api/products/{pid}", status_code=204)
async def delete_product(pid: int):
    r = await execute_db(get_supabase().table("products").delete().eq("id", pid))
    if not r.data: raise HTTPException(404, "Not found")

# Categories
@app.get("/api/categories", response_model=List[Category])
async def get_categories():
    r = await execute_db(get_supabase().table("categories").select("*").order("name"))
    return r.data

@app.post("/api/categories", response_model=Category, status_code=201)
async def create_category(c: CategoryBase):
    r = await execute_db(get_supabase().table("categories").insert(c.dict()))
    if not r.data: raise HTTPException(400, "Failed")
    return r.data[0]

@app.put("/api/categories/{cid}")
async def update_category(cid: int, c: CategoryBase):
    r = await execute_db(get_supabase().table("categories").update(c.dict()).eq("id", cid))
    if not r.data: raise HTTPException(404, "Not found")
    return r.data[0]

@app.delete("/api/categories/{cid}", status_code=204)
async def delete_category(cid: int):
    r = await execute_db(get_supabase().table("categories").delete().eq("id", cid))
    if not r.data: raise HTTPException(404, "Not found")

# ---------- ORDERS (UPDATED) ----------
@app.get("/api/orders", response_model=List[Dict])
async def get_orders():
    r = await execute_db(get_supabase().table("orders").select("*").order("created_at", desc=True))
    for o in r.data:
        o["itemCount"] = len(o.get("items", []))
    return r.data

@app.get("/api/orders/{oid}")
async def get_order(oid: int):
    r = await execute_db(get_supabase().table("orders").select("*").eq("id", oid))
    if not r.data: raise HTTPException(404, "Not found")
    return r.data[0]

@app.post("/api/orders", status_code=201)
async def create_order(order: OrderCreate, bg: BackgroundTasks):
    # Insert the order into Supabase (status = "pending")
    data = order.dict(exclude={'monnify_transaction_ref'})
    result = await execute_db(get_supabase().table("orders").insert(data))
    if not result.data:
        raise HTTPException(400, "Failed to create order")
    
    order_data = result.data[0]
    
    # Initialize Monnify transaction
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
        # Rollback order creation
        await execute_db(get_supabase().table("orders").delete().eq("id", order_data["id"]))
        raise HTTPException(400, f"Payment initialization failed: {monnify_result.get('error', 'Unknown error')}")
    
    # Update the order with Monnify transaction reference
    await execute_db(
        get_supabase().table("orders")
        .update({"monnify_transaction_ref": monnify_result["transaction_reference"]})
        .eq("id", order_data["id"])
    )
    
    # Send email in background
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
    if not r.data: raise HTTPException(404, "Not found")
    return r.data[0]

@app.get("/api/stats")
async def get_stats():
    return await get_cached_stats()

# Banners
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
    if not b: raise HTTPException(404, "Not found")
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

# ---------- WEBHOOK (UPDATED with stock reduction and cache invalidation) ----------
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
        payment_ref = data.get("paymentReference")  # our custom ref

        # Find the order
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
                # Fetch current stock
                prod_result = await execute_db(
                    get_supabase().table("products").select("stock").eq("id", product_id)
                )
                if prod_result.data:
                    current_stock = prod_result.data[0].get("stock", 0)
                    new_stock = max(0, current_stock - qty)  # prevent negative
                    # Update stock
                    await execute_db(
                        get_supabase().table("products")
                        .update({"stock": new_stock})
                        .eq("id", product_id)
                    )
                    logger.info(f"Stock updated for product {product_id}: {current_stock} → {new_stock}")

        # Invalidate stats cache after stock changes
        invalidate_stats_cache()

        # Update order status to paid (if not already)
        if order.get("status") != "paid":
            await execute_db(
                get_supabase().table("orders")
                .update({"status": "paid"})
                .eq("id", order["id"])
            )

    return {"status": "received"}

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
