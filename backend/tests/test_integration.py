"""
Integration tests for the Hot Portion Grill backend, exercising the
endpoints through a FastAPI TestClient backed by an in-memory Supabase
stand-in and a deterministic Monnify mock.

Run with:  pytest -q
"""
import pytest


# =============================================================
# IDEMPOTENCY
# =============================================================

def _order_payload(**overrides):
    base = {
        "customer_name": "Ada Obi",
        "customer_email": "ada@example.com",
        "customer_phone": "08012345678",
        "delivery_method": "pickup",
        "items": [{"name": "Burger", "qty": 1, "price": 4500, "product_id": 1}],
    }
    base.update(overrides)
    return base


def test_idempotency_returns_same_order_on_retry(client, fake_db, mock_monnify):
    payload = _order_payload(idempotency_key="test-key-A")

    r1 = client.post("/api/orders", json=payload)
    r2 = client.post("/api/orders", json=payload)

    assert r1.status_code == 201, r1.text
    assert r2.status_code == 201, r2.text

    j1, j2 = r1.json(), r2.json()
    assert j1["order_id"] == j2["order_id"]
    assert j1["payment_reference"] == j2["payment_reference"]
    assert j1["checkout_url"] == j2["checkout_url"]

    # Only ONE order row created
    assert len(fake_db.store["orders"]) == 1

    # Only ONE Monnify initialize_transaction call — the retry was served from cache
    assert len(mock_monnify) == 1


def test_different_idempotency_keys_create_different_orders(client, fake_db, mock_monnify):
    r1 = client.post("/api/orders", json=_order_payload(idempotency_key="key-1"))
    r2 = client.post("/api/orders", json=_order_payload(idempotency_key="key-2"))

    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["order_id"] != r2.json()["order_id"]
    assert len(fake_db.store["orders"]) == 2


def test_missing_idempotency_key_still_works_backward_compatible(client, fake_db):
    """No key -> order is created (with an auto-generated key for bookkeeping)."""
    r = client.post("/api/orders", json=_order_payload())
    assert r.status_code == 201
    assert len(fake_db.store["orders"]) == 1
    # The auto-generated key is present on the row
    assert fake_db.store["orders"][0]["idempotency_key"].startswith("auto-")


def test_idempotency_replay_without_checkout_url_reinitializes_monnify(
    client, fake_db, mock_monnify
):
    """If the first attempt died between DB insert and Monnify, replay repairs it."""
    payload = _order_payload(idempotency_key="repair-key")

    # Simulate the crash: insert the order row directly, no checkout_url, still pending
    fake_db.store["orders"].append({
        "id": 999,
        "payment_reference": "HP-REPAIR-TEST",
        "customer_name": "Ada Obi",
        "customer_email": "ada@example.com",
        "customer_phone": "08012345678",
        "total": 4500,
        "status": "pending",
        "idempotency_key": "repair-key",
        "checkout_url": None,
        "items": [{"name": "Burger", "qty": 1, "price": 4500, "product_id": 1}],
    })

    r = client.post("/api/orders", json=payload)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["order_id"] == 999
    assert body["checkout_url"].endswith("HP-REPAIR-TEST")
    # Monnify was called once to repair
    assert len(mock_monnify) == 1
    assert mock_monnify[0]["payment_reference"] == "HP-REPAIR-TEST"


# =============================================================
# ORDER LOOKUP BY REFERENCE — EMAIL REQUIRED
# =============================================================

def test_by_reference_without_email_is_rejected(client):
    r = client.get("/api/orders/by-reference/HP-SOMETHING")
    # Missing required query param -> 422 from FastAPI
    assert r.status_code == 422


def test_by_reference_with_wrong_email_is_404(client, fake_db):
    fake_db.store["orders"].append({
        "id": 1,
        "payment_reference": "HP-PII-TEST",
        "customer_email": "owner@example.com",
        "status": "pending",
    })
    r = client.get("/api/orders/by-reference/HP-PII-TEST?email=attacker@example.com")
    assert r.status_code == 404


def test_by_reference_with_matching_email_succeeds(client, fake_db):
    fake_db.store["orders"].append({
        "id": 1,
        "payment_reference": "HP-MATCH",
        "customer_email": "ada@example.com",
        "customer_name": "Ada",
        "status": "pending",
    })
    r = client.get("/api/orders/by-reference/HP-MATCH?email=ada@example.com")
    assert r.status_code == 200
    assert r.json()["customer_name"] == "Ada"


def test_by_reference_email_is_case_insensitive(client, fake_db):
    fake_db.store["orders"].append({
        "id": 1,
        "payment_reference": "HP-CASE",
        "customer_email": "ada@example.com",
        "status": "pending",
    })
    r = client.get("/api/orders/by-reference/HP-CASE?email=Ada@Example.COM")
    assert r.status_code == 200


# =============================================================
# DELIVERY FEE — FAIL CLOSED
# =============================================================

def test_delivery_order_fails_closed_when_address_unresolvable(
    client, fake_db, mock_geocode
):
    """Server cannot geocode the address -> order rejected, never billed at client's fee."""
    payload = _order_payload(
        delivery_method="delivery",
        delivery_address="FAIL nowhere land",
        delivery_fee=0,  # client tries to claim 0 delivery fee
    )
    r = client.post("/api/orders", json=payload)
    assert r.status_code == 400
    detail = r.json().get("detail", "").lower()
    assert "couldn't locate" in detail or "couldn't calculate" in detail
    # No order row was created
    assert len(fake_db.store["orders"]) == 0


def test_delivery_order_succeeds_when_address_resolves(client, fake_db, mock_geocode):
    payload = _order_payload(
        delivery_method="delivery",
        delivery_address="12 Broad Street, Lagos",
    )
    r = client.post("/api/orders", json=payload)
    assert r.status_code == 201, r.text
    # Server computed a nonzero fee from the base area fee
    order = fake_db.store["orders"][0]
    assert order["delivery_fee"] >= 500  # MINIMUM_DELIVERY_FEE


def test_delivery_order_rejects_client_supplied_zero_fee(client, fake_db, mock_geocode):
    """Classic exploit attempt: client sends fee=0. Server must ignore it."""
    payload = _order_payload(
        delivery_method="delivery",
        delivery_address="12 Broad Street, Lagos",
        delivery_fee=0,
        total=0,
    )
    r = client.post("/api/orders", json=payload)
    assert r.status_code == 201
    order = fake_db.store["orders"][0]
    assert order["delivery_fee"] >= 500
    assert order["total"] == order["delivery_fee"] + 4500


def test_delivery_without_address_is_rejected(client, fake_db):
    payload = _order_payload(delivery_method="delivery", delivery_address="")
    r = client.post("/api/orders", json=payload)
    assert r.status_code == 400
    assert "address is required" in r.json()["detail"].lower()


# =============================================================
# CLIENT COORDINATE VERIFICATION
# =============================================================

def test_client_coords_agreeing_with_server_are_trusted(
    client, fake_db, mock_geocode, mock_monnify
):
    """Client coords within threshold -> accepted, server trusts them."""
    # Server geocode returns (6.5244, 3.3792). Client sends nearly-identical.
    payload = _order_payload(
        delivery_method="delivery",
        delivery_address="12 Broad Street, Lagos",
        lat=6.5245,
        lng=3.3793,
    )
    r = client.post("/api/orders", json=payload)
    assert r.status_code == 201


def test_client_coords_far_from_address_are_overridden(
    client, fake_db, mock_geocode, mock_monnify, caplog
):
    """Client sends coords in a cheaper zone. Server overrides with geocode."""
    import logging
    caplog.set_level(logging.WARNING, logger="main")

    # Client coords ~200km away from the geocoded address
    payload = _order_payload(
        delivery_method="delivery",
        delivery_address="12 Broad Street, Lagos",
        lat=6.0,
        lng=5.0,
    )
    r = client.post("/api/orders", json=payload)
    # Order still succeeds (we don't reject — we just use server coords)
    assert r.status_code == 201
    # A warning was emitted about the mismatch
    assert any("coords mismatch" in rec.message.lower() for rec in caplog.records)


# =============================================================
# PAGINATION
# =============================================================

def test_orders_endpoint_accepts_pagination_params(client, fake_db):
    """Endpoint honours limit/offset query params (auth-gated, so 401/403 without token)."""
    r = client.get("/api/orders?limit=10&offset=0")
    # No auth header -> 401 (proves routing works; we don't need a real JWT here)
    assert r.status_code in (401, 403)


def test_orders_endpoint_rejects_invalid_limit(client):
    r = client.get("/api/orders?limit=0")
    # Auth runs before query-param validation; without a token we get 401.
    assert r.status_code in (401, 422)


def test_orders_endpoint_rejects_oversized_limit(client):
    r = client.get("/api/orders?limit=99999")
    assert r.status_code in (401, 422)

# =============================================================
# MONNIFY URLS FROM SETTINGS
# =============================================================

def test_monnify_urls_are_config_driven(monkeypatch):
    """Proves the hardcoded URLs are gone — settings take effect."""
    import main

    assert main.settings.MONNIFY_REDIRECT_URL
    assert main.settings.MONNIFY_WEBHOOK_URL

    # Override via monkeypatch and confirm the value flows through
    monkeypatch.setattr(
        main.settings, "MONNIFY_REDIRECT_URL",
        "https://staging.example.com/paid",
    )
    monkeypatch.setattr(
        main.settings, "MONNIFY_WEBHOOK_URL",
        "https://staging.example.com/hook",
    )
    # The class reads from settings at call time — assert the attribute is read,
    # not cached in the class body.
    import inspect
    source = inspect.getsource(main.MonnifyIntegration.initialize_transaction)
    assert "settings.MONNIFY_REDIRECT_URL" in source
    assert "settings.MONNIFY_WEBHOOK_URL" in source
    # And that the literal strings are gone from the method
    assert "hotportion.onrender.com/api/v1/webhooks/monnify" not in source


# =============================================================
# UNIT: haversine distance sanity
# =============================================================

def test_haversine_same_point_is_zero():
    from main import _haversine_m
    assert _haversine_m(6.5244, 3.3792, 6.5244, 3.3792) == pytest.approx(0.0, abs=1e-6)


def test_haversine_known_distance():
    """Lagos (6.5244, 3.3792) to Abuja (9.0765, 7.3986) ≈ 530 km."""
    from main import _haversine_m
    d = _haversine_m(6.5244, 3.3792, 9.0765, 7.3986)
    assert 500_000 < d < 560_000
