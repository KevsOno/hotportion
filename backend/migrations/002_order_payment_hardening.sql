-- Hot Portion Grill order/payment hardening migration.
-- Safe for an existing database. Run once in Supabase SQL Editor if the
-- deployment already predates this migration.
--
-- IMPORTANT BUSINESS RULE:
-- Stock is intentionally allowed to reach zero while orders continue.
-- We are tracking sales against known stock, not preventing sales because
-- the current stock figure is insufficient.

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS idempotency_key TEXT;

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS payment_method TEXT DEFAULT 'online';

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS delivery_fee INTEGER DEFAULT 0;

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS cancellation_reason TEXT;

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS delivery_breakdown JSONB;

ALTER TABLE orders
    DROP CONSTRAINT IF EXISTS orders_status_check;

ALTER TABLE orders
    ADD CONSTRAINT orders_status_check
    CHECK (
        status IN (
            'pending',
            'awaiting_payment',
            'paid',
            'confirmed',
            'completed',
            'cancelled'
        )
    );

CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_idempotency_key
    ON orders (idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_orders_payment_reference
    ON orders (payment_reference);

CREATE INDEX IF NOT EXISTS idx_orders_monnify_transaction_ref
    ON orders (monnify_transaction_ref);

CREATE INDEX IF NOT EXISTS idx_orders_status_created_at
    ON orders (status, created_at);

CREATE OR REPLACE FUNCTION transition_order_and_stock(
    p_order_id INTEGER,
    p_from_status TEXT,
    p_to_status TEXT,
    p_cancellation_reason TEXT DEFAULT NULL
)
RETURNS SETOF orders AS $$
DECLARE
    v_order orders%ROWTYPE;
    v_item JSONB;
    v_product_id INTEGER;
    v_qty INTEGER;
BEGIN
    IF p_from_status IS NULL OR p_to_status IS NULL THEN
        RAISE EXCEPTION 'Order transition statuses are required';
    END IF;

    IF p_from_status = p_to_status THEN
        RAISE EXCEPTION 'No-op order transition is not allowed';
    END IF;

    IF NOT (
        (p_from_status = 'pending' AND p_to_status IN ('paid', 'confirmed', 'cancelled'))
        OR (p_from_status = 'awaiting_payment' AND p_to_status IN ('paid', 'confirmed', 'completed', 'cancelled'))
        OR (p_from_status = 'paid' AND p_to_status IN ('confirmed', 'completed', 'cancelled'))
        OR (p_from_status = 'confirmed' AND p_to_status IN ('completed', 'cancelled'))
    ) THEN
        RAISE EXCEPTION 'Invalid order status transition: % -> %', p_from_status, p_to_status;
    END IF;

    SELECT * INTO v_order
    FROM orders
    WHERE id = p_order_id
      AND status = p_from_status
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN;
    END IF;

    IF p_to_status IN ('paid', 'confirmed')
       AND p_from_status NOT IN ('paid', 'confirmed') THEN
        FOR v_item IN
            SELECT value
            FROM jsonb_array_elements(COALESCE(v_order.items, '[]'::jsonb))
        LOOP
            v_product_id := NULLIF(v_item->>'product_id', '')::INTEGER;
            v_qty := NULLIF(v_item->>'qty', '')::INTEGER;

            IF v_product_id IS NOT NULL AND v_qty IS NOT NULL AND v_qty > 0 THEN
                UPDATE products
                SET stock = GREATEST(0, stock - v_qty)
                WHERE id = v_product_id;
            END IF;
        END LOOP;

    ELSIF p_to_status = 'cancelled'
          AND p_from_status IN ('paid', 'confirmed') THEN
        FOR v_item IN
            SELECT value
            FROM jsonb_array_elements(COALESCE(v_order.items, '[]'::jsonb))
        LOOP
            v_product_id := NULLIF(v_item->>'product_id', '')::INTEGER;
            v_qty := NULLIF(v_item->>'qty', '')::INTEGER;

            IF v_product_id IS NOT NULL AND v_qty IS NOT NULL AND v_qty > 0 THEN
                UPDATE products
                SET stock = stock + v_qty
                WHERE id = v_product_id;
            END IF;
        END LOOP;
    END IF;

    UPDATE orders
    SET status = p_to_status,
        cancellation_reason = CASE
            WHEN p_cancellation_reason IS NOT NULL
            THEN p_cancellation_reason
            ELSE cancellation_reason
        END
    WHERE id = p_order_id
      AND status = p_from_status
    RETURNING * INTO v_order;

    IF NOT FOUND THEN
        RETURN;
    END IF;

    RETURN NEXT v_order;
    RETURN;
END;
$ LANGUAGE plpgsql;

REVOKE ALL ON FUNCTION transition_order_and_stock(INTEGER, TEXT, TEXT, TEXT)
FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION transition_order_and_stock(INTEGER, TEXT, TEXT, TEXT)
TO service_role;


-- Harden legacy stock helper RPCs. They are not used to decide whether an
-- order may be accepted; they only mutate inventory when explicitly called.
-- Reject non-positive quantities so malformed calls cannot increase/decrease
-- stock in the wrong direction.

CREATE OR REPLACE FUNCTION public.decrement_product_stock(
    p_product_id INTEGER,
    p_qty INTEGER
)
RETURNS void AS $$
BEGIN
    IF p_qty IS NULL OR p_qty <= 0 THEN
        RAISE EXCEPTION 'Stock decrement quantity must be positive';
    END IF;

    UPDATE products
    SET stock = GREATEST(0, stock - p_qty)
    WHERE id = p_product_id;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION public.increment_product_stock(
    p_product_id INTEGER,
    p_qty INTEGER
)
RETURNS void AS $$
BEGIN
    IF p_qty IS NULL OR p_qty <= 0 THEN
        RAISE EXCEPTION 'Stock increment quantity must be positive';
    END IF;

    UPDATE products
    SET stock = stock + p_qty
    WHERE id = p_product_id;
END;
$$ LANGUAGE plpgsql;

REVOKE EXECUTE ON FUNCTION public.decrement_product_stock(INTEGER, INTEGER)
FROM PUBLIC, anon, authenticated;

REVOKE EXECUTE ON FUNCTION public.increment_product_stock(INTEGER, INTEGER)
FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION public.decrement_product_stock(INTEGER, INTEGER)
TO service_role;

GRANT EXECUTE ON FUNCTION public.increment_product_stock(INTEGER, INTEGER)
TO service_role;
