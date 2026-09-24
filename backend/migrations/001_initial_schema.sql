-- ───────────────────────────────────────────────────────────────
-- HOT PORTION GRILL — Complete Database Schema
-- Supabase / PostgreSQL
-- ───────────────────────────────────────────────────────────────

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ─── 1. CATEGORIES ───
CREATE TABLE IF NOT EXISTS categories (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ─── 2. PRODUCTS ───
CREATE TABLE IF NOT EXISTS products (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    price INTEGER NOT NULL CHECK (price >= 0),
    stock INTEGER DEFAULT 0 CHECK (stock >= 0),
    tag TEXT NOT NULL,
    emoji TEXT DEFAULT '🍽️',
    image TEXT,
    tag_color TEXT DEFAULT 'primary',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ─── 3. ORDERS ───
CREATE TABLE IF NOT EXISTS orders (
    id SERIAL PRIMARY KEY,
    payment_reference TEXT UNIQUE NOT NULL,
    customer_name TEXT NOT NULL,
    customer_email TEXT NOT NULL,
    customer_phone TEXT NOT NULL,
    total INTEGER NOT NULL,
    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'confirmed', 'preparing', 'ready', 'completed', 'cancelled')),
    delivery_method TEXT DEFAULT 'pickup' CHECK (delivery_method IN ('pickup', 'delivery', 'dinein')),
    delivery_address TEXT,
    preferred_time TEXT,
    order_notes TEXT,
    items JSONB NOT NULL DEFAULT '[]',
    idempotency_key TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ─── 4. BANNERS ───
CREATE TABLE IF NOT EXISTS banners (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    subtitle TEXT,
    description TEXT,
    image_url TEXT,
    cta_text TEXT,
    cta_link TEXT,
    cta_type TEXT DEFAULT 'button' CHECK (cta_type IN ('button', 'link', 'product')),
    product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
    badge_text TEXT,
    badge_color TEXT DEFAULT '#E53935',
    background_color TEXT DEFAULT '#fff3ed',
    text_color TEXT DEFAULT '#1e1e1e',
    position INTEGER DEFAULT 0,
    is_active BOOLEAN DEFAULT TRUE,
    is_hero BOOLEAN DEFAULT FALSE,
    is_featured BOOLEAN DEFAULT FALSE,
    display_order INTEGER DEFAULT 0,
    start_date TIMESTAMPTZ,
    end_date TIMESTAMPTZ,
    discount_type TEXT CHECK (discount_type IN ('percentage', 'fixed', NULL)),
    discount_value INTEGER,
    meta_data JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ─── 5. BANNER CATEGORIES (many-to-many) ───
CREATE TABLE IF NOT EXISTS banner_categories (
    banner_id INTEGER REFERENCES banners(id) ON DELETE CASCADE,
    category_id INTEGER REFERENCES categories(id) ON DELETE CASCADE,
    PRIMARY KEY (banner_id, category_id)
);

-- ─── 6. BANNER PRODUCTS (many-to-many) ───
CREATE TABLE IF NOT EXISTS banner_products (
    banner_id INTEGER REFERENCES banners(id) ON DELETE CASCADE,
    product_id INTEGER REFERENCES products(id) ON DELETE CASCADE,
    PRIMARY KEY (banner_id, product_id)
);

-- ─── 7. INDEXES (for performance) ───
CREATE INDEX IF NOT EXISTS idx_products_tag ON products(tag);
CREATE INDEX IF NOT EXISTS idx_products_name ON products(name);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_payment_ref ON orders(payment_reference);
CREATE INDEX IF NOT EXISTS idx_banners_is_active ON banners(is_active);
CREATE INDEX IF NOT EXISTS idx_banners_is_hero ON banners(is_hero);
CREATE INDEX IF NOT EXISTS idx_banners_display_order ON banners(display_order);
CREATE INDEX IF NOT EXISTS idx_banners_start_end ON banners(start_date, end_date);
CREATE INDEX IF NOT EXISTS idx_banner_categories_banner ON banner_categories(banner_id);
CREATE INDEX IF NOT EXISTS idx_banner_products_banner ON banner_products(banner_id);

-- ─── 8. TRIGGERS (auto-update timestamps) ───
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_products_updated_at
    BEFORE UPDATE ON products
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER trigger_orders_updated_at
    BEFORE UPDATE ON orders
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER trigger_banners_updated_at
    BEFORE UPDATE ON banners
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- ─── 9. RLS (Row Level Security) Policies ───
-- Enable RLS on all tables
ALTER TABLE products ENABLE ROW LEVEL SECURITY;
ALTER TABLE categories ENABLE ROW LEVEL SECURITY;
ALTER TABLE orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE banners ENABLE ROW LEVEL SECURITY;
ALTER TABLE banner_categories ENABLE ROW LEVEL SECURITY;
ALTER TABLE banner_products ENABLE ROW LEVEL SECURITY;

-- Public read access to products, categories, banners
CREATE POLICY "Public read products" ON products FOR SELECT USING (true);
CREATE POLICY "Public read categories" ON categories FOR SELECT USING (true);
CREATE POLICY "Public read banners" ON banners FOR SELECT USING (true);
CREATE POLICY "Public read banner_categories" ON banner_categories FOR SELECT USING (true);
CREATE POLICY "Public read banner_products" ON banner_products FOR SELECT USING (true);

-- Admin full access (using service role key) — handled by Supabase service role bypass.
-- For non-service users, we restrict write operations.

-- Orders: public can insert, but only admin (service role) can read/update
CREATE POLICY "Public insert orders" ON orders FOR INSERT WITH CHECK (true);
CREATE POLICY "Admin all orders" ON orders FOR ALL USING (
    current_setting('request.jwt.claims', true)::json->>'role' = 'service_role'
);

-- ─── 10. SEED DATA (optional) ───
INSERT INTO categories (name) VALUES 
    ('Burgers'), ('Rice Dishes'), ('Swallow & Soups'), 
    ('Specialties'), ('Grilled & Roast'), ('Beans, Yam & Plantain'), 
    ('Snacks'), ('Beverages')
ON CONFLICT (name) DO NOTHING;

-- ───────────────────────────────────────────────────────────────
-- END MIGRATION
-- ───────────────────────────────────────────────────────────────


-- Idempotency key is unique per checkout attempt. NULL is allowed for
-- legacy/manual rows and clients that do not send a key.
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_idempotency_key
    ON orders (idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- Atomic payment/order state transition and stock ledger update.
-- Stock intentionally bottoms out at zero. Orders are never rejected merely
-- because current stock is insufficient.
CREATE OR REPLACE FUNCTION transition_order_and_stock(
    p_order_id BIGINT,
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
$$ LANGUAGE plpgsql;

