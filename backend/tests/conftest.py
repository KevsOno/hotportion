"""
Pytest fixtures for the Hot Portion Grill backend.

Sets dummy env vars before importing main, then exposes:
  - fake_db      : in-memory Supabase stand-in (works for the fluent API)
  - mock_monnify : replaces MonnifyIntegration.initialize_transaction
  - mock_geocode : replaces geocode_address with a deterministic result
  - client       : TestClient with lifespan running but external services stubbed
"""
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

# ---- Required settings (dummies) — must be set BEFORE importing main ----
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-role-key")
os.environ.setdefault("BREVO_API_KEY", "test-brevo-key")
os.environ.setdefault("BREVO_SENDER_EMAIL", "no-reply@example.com")
os.environ.setdefault("MONNIFY_API_KEY", "test-monnify-api-key")
os.environ.setdefault("MONNIFY_SECRET_KEY", "test-monnify-secret-key")
os.environ.setdefault("MONNIFY_CONTRACT_CODE", "test-contract-code")
os.environ.setdefault("RECONCILIATION_ENABLED", "false")
os.environ.setdefault("NOMINATIM_FALLBACK_ENABLED", "false")
os.environ.setdefault("SENTRY_DSN", "")
os.environ.setdefault("SENTRY_DISABLED", "1")

# Make the project root importable
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402


# ────────────────────────────────────────────────────────────────────
# Autouse: wipe any in-process rate limiter state between tests.
# Inspects `main` at runtime so it doesn't need to know the variable name.
# ────────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _reset_rate_limits():
    def _wipe():
        for name, val in list(vars(main).items()):
            if isinstance(val, dict) and not name.isupper():
                lname = name.lower()
                if any(k in lname for k in ("rate", "bucket", "limit", "hits", "throttle")):
                    try:
                        val.clear()
                    except Exception:
                        pass
    _wipe()
    yield
    _wipe()


# ────────────────────────────────────────────────────────────────────
# Fake Supabase
# ────────────────────────────────────────────────────────────────────
class _FakeResult:
    def __init__(self, data=None, count=None):
        self.data = list(data) if data is not None else []
        self.count = count if count is not None else len(self.data)


class _FakeRpc:
    def __init__(self, name, params, store):
        self.name = name
        self.params = params or {}
        self.store = store

    def execute(self):
        name = self.name
        p = self.params

        if name == "find_delivery_area":
            areas = self.store.get("delivery_areas", [])
            return _FakeResult([dict(areas[0])] if areas else [])

        if name == "transition_order_and_stock":
            oid = p.get("p_order_id")
            from_status = p.get("p_from_status")
            to_status = p.get("p_to_status")
            order = next((o for o in self.store.get("orders", []) if o.get("id") == oid), None)
            if not order or order.get("status") != from_status:
                return _FakeResult([])
            order["status"] = to_status
            if p.get("p_cancellation_reason") is not None:
                order["cancellation_reason"] = p.get("p_cancellation_reason")
            if from_status in ("pending", "awaiting_payment") and to_status in ("paid", "confirmed"):
                for item in order.get("items", []):
                    for prod in self.store.get("products", []):
                        if prod.get("id") == item.get("product_id"):
                            prod["stock"] = max(0, int(prod.get("stock") or 0) - int(item.get("qty") or 0))
            elif to_status == "cancelled" and from_status in ("paid", "confirmed"):
                for item in order.get("items", []):
                    for prod in self.store.get("products", []):
                        if prod.get("id") == item.get("product_id"):
                            prod["stock"] = int(prod.get("stock") or 0) + int(item.get("qty") or 0)
            return _FakeResult([dict(order)])

        if name == "decrement_product_stock":
            pid = p.get("p_product_id")
            qty = int(p.get("p_qty") or 0)
            for prod in self.store.get("products", []):
                if prod.get("id") == pid:
                    prod["stock"] = max(0, int(prod.get("stock") or 0) - qty)
                    break
            return _FakeResult([{"ok": True}])

        if name == "increment_product_stock":
            pid = p.get("p_product_id")
            qty = int(p.get("p_qty") or 0)
            for prod in self.store.get("products", []):
                if prod.get("id") == pid:
                    prod["stock"] = int(prod.get("stock") or 0) + qty
                    break
            return _FakeResult([{"ok": True}])

        return _FakeResult([])


class _FakeTable:
    def __init__(self, store, name):
        self.store = store
        self.name = name
        self._mode = None
        self._payload = None
        self._filters = []
        self._order_col = None
        self._desc = False
        self._limit = None
        self._range = None
        self._count_exact = False
        self._single = False
        self._maybe_single = False

    def select(self, *cols, count=None):
        self._mode = "select"
        if count == "exact":
            self._count_exact = True
        return self

    def insert(self, payload):
        self._mode = "insert"
        self._payload = payload
        return self

    def update(self, payload):
        self._mode = "update"
        self._payload = payload
        return self

    def delete(self):
        self._mode = "delete"
        return self

    def eq(self, col, val):
        self._filters.append((col, "eq", val))
        return self

    def neq(self, col, val):
        self._filters.append((col, "neq", val))
        return self

    def in_(self, col, vals):
        self._filters.append((col, "in", list(vals)))
        return self

    def gt(self, col, val):
        self._filters.append((col, "gt", val))
        return self

    def lt(self, col, val):
        self._filters.append((col, "lt", val))
        return self

    def order(self, col, desc=False):
        self._order_col = col
        self._desc = desc
        return self

    def limit(self, n):
        self._limit = n
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def single(self):
        self._single = True
        return self

    def maybe_single(self):
        self._maybe_single = True
        return self

    def _match(self, row):
        for col, op, val in self._filters:
            rv = row.get(col)
            if op == "eq" and rv != val:
                return False
            if op == "neq" and rv == val:
                return False
            if op == "in" and rv not in val:
                return False
            if op == "gt" and not (rv is not None and rv > val):
                return False
            if op == "lt" and not (rv is not None and rv < val):
                return False
        return True

    def execute(self):
        rows = self.store.setdefault(self.name, [])

        if self._mode == "select":
            matched = [r for r in rows if self._match(r)]
            if self._order_col:
                matched.sort(key=lambda r: r.get(self._order_col, ""), reverse=self._desc)
            count = len(matched)
            if self._range is not None:
                s, e = self._range
                matched = matched[s:e + 1]
            if self._limit is not None:
                matched = matched[:self._limit]

            if self._single:
                if len(matched) != 1:
                    raise Exception(
                        f"JSON object requested, multiple (or no) rows returned (got {len(matched)})"
                    )
                return _FakeResult(matched[0])
            if self._maybe_single:
                if len(matched) == 0:
                    return _FakeResult(None)
                if len(matched) > 1:
                    raise Exception("multiple rows returned for maybe_single")
                return _FakeResult(matched[0])

            return _FakeResult(matched, count=count if self._count_exact else None)

        if self._mode == "insert":
            payload = self._payload if isinstance(self._payload, list) else [self._payload]
            if self.name == "orders":
                for row in payload:
                    key = row.get("idempotency_key")
                    if key:
                        for existing in rows:
                            if existing.get("idempotency_key") == key:
                                raise Exception(
                                    'duplicate key value violates unique constraint '
                                    '"idx_orders_idempotency_key"'
                                )
            out = []
            for row in payload:
                row = dict(row)
                if "id" not in row:
                    row["id"] = len(rows) + 1
                rows.append(row)
                out.append(row)
            return _FakeResult(out)

        if self._mode == "update":
            matched = [r for r in rows if self._match(r)]
            for r in matched:
                r.update(self._payload or {})
            return _FakeResult(matched)

        if self._mode == "delete":
            matched = [r for r in rows if self._match(r)]
            for r in matched:
                rows.remove(r)
            return _FakeResult(matched)

        return _FakeResult([])


class _FakeSupabase:
    def __init__(self):
        self.store = {}

    def table(self, name):
        return _FakeTable(self.store, name)

    def rpc(self, name, params=None):
        return _FakeRpc(name, params, self.store)


@pytest.fixture
def fake_db(monkeypatch):
    fake = _FakeSupabase()
    fake.store["products"] = [
        {"id": 1, "name": "Burger", "price": 4500, "stock": 10,
         "is_main_item": True, "weight_kg": 0.5, "is_bulky": False},
        {"id": 2, "name": "Zobo", "price": 800, "stock": 20,
         "is_main_item": False, "weight_kg": 0.3, "is_bulky": False},
    ]
    fake.store["delivery_areas"] = [{"id": 1, "name": "Central", "fee": 1000}]
    fake.store["orders"] = []
    monkeypatch.setattr(main, "get_supabase", lambda: fake)
    return fake


@pytest.fixture
def mock_monnify(monkeypatch):
    calls = []

    async def fake_init(self, amount, customer_name, customer_email,
                        customer_phone, payment_reference,
                        payment_description="Hot Portion Grill Order"):
        calls.append({
            "amount": amount,
            "payment_reference": payment_reference,
            "customer_email": customer_email,
        })
        return {
            "success": True,
            "transaction_reference": f"TXN-{payment_reference}",
            "checkout_url": f"https://sandbox.monnify.com/checkout/{payment_reference}",
        }

    monkeypatch.setattr(main.MonnifyIntegration, "initialize_transaction", fake_init)
    return calls


@pytest.fixture
def mock_geocode(monkeypatch):
    async def fake_geocode(address):
        if not address or address.upper().startswith("FAIL"):
            return None
        return (6.5244, 3.3792)

    monkeypatch.setattr(main, "geocode_address", fake_geocode)
    return fake_geocode


@pytest.fixture
def client(fake_db, monkeypatch, mock_monnify):
    """
    TestClient with external services stubbed.

    - depends on fake_db so get_supabase is patched BEFORE the lifespan fires.
    - fresh ThreadPoolExecutor per test, because main.py's lifespan shuts down
      the module-level _executor on first teardown, which would otherwise
      kill every subsequent test with
      'RuntimeError: cannot schedule new futures after shutdown'.
    - neutralises setup_database() so the lifespan never builds a real
      Supabase client with the dummy service key.
    """
    async def noop(self):
        return None

    monkeypatch.setattr(main.BrevoIntegration, "initialize", noop)
    monkeypatch.setattr(main.MonnifyIntegration, "initialize", noop)

    fresh_executor = ThreadPoolExecutor(max_workers=4)
    monkeypatch.setattr(main, "_executor", fresh_executor, raising=False)

    if hasattr(main, "setup_database"):
        async def _noop_setup():
            return None
        monkeypatch.setattr(main, "setup_database", _noop_setup, raising=False)

    with TestClient(main.app) as c:
        yield c

    try:
        fresh_executor.shutdown(wait=False)
    except Exception:
        pass
