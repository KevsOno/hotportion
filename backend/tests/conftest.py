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

# Make the project root importable
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402


# ────────────────────────────────────────────────────────────────────
# Fake Supabase — minimal in-memory implementation covering the fluent
# query API used in main.py: select/insert/update/delete + eq/neq/in_/gt/lt
# + order/limit/range + count="exact", plus a couple of RPCs.
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

        # Unknown RPC (exec_sql, etc.) — succeed silently
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

    # ── fluent API ──
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

    # ── internal ──
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
            return _FakeResult(matched, count=count if self._count_exact else None)

        if self._mode == "insert":
            payload = self._payload if isinstance(self._payload, list) else [self._payload]
            # Simulate unique constraint on orders.idempotency_key
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
    # Seed baseline data
    fake.store["products"] = [
        {
            "id": 1, "name": "Burger", "price": 4500, "stock": 10,
            "is_main_item": True, "weight_kg": 0.5, "is_bulky": False,
        },
        {
            "id": 2, "name": "Zobo", "price": 800, "stock": 20,
            "is_main_item": False, "weight_kg": 0.3, "is_bulky": False,
        },
    ]
    fake.store["delivery_areas"] = [
        {"id": 1, "name": "Central", "fee": 1000},
    ]
    fake.store["orders"] = []
    monkeypatch.setattr(main, "get_supabase", lambda: fake)
    return fake


@pytest.fixture
def mock_monnify(monkeypatch):
    """Replace the outbound Monnify call with a deterministic fake."""
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
    """Return fixed coords for any address, unless the address starts with FAIL."""
    async def fake_geocode(address):
        if not address or address.upper().startswith("FAIL"):
            return None
        return (6.5244, 3.3792)  # Lagos

    monkeypatch.setattr(main, "geocode_address", fake_geocode)
    return fake_geocode


@pytest.fixture
def client(monkeypatch, mock_monnify):
    """FastAPI TestClient with external service init stubbed."""
    async def noop(self):
        return None
    monkeypatch.setattr(main.BrevoIntegration, "initialize", noop)
    monkeypatch.setattr(main.MonnifyIntegration, "initialize", noop)
    with TestClient(main.app) as c:
        yield c
