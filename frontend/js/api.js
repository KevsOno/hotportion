import { API_BASE, DELIVERY_FEE_MAX_RETRIES, DELIVERY_FEE_RETRY_DELAY_MS } from './config.js';
import { state } from './state.js';
import { dom } from './dom.js';
import { renderHeroSlides } from './hero.js';

// ─── AMAZON LOCATION SERVICE via backend proxy ───
export async function amazonPlacesFetch(endpoint, body) {
    const response = await fetch(`${API_BASE}/api/places/${endpoint}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    if (!response.ok) {
        const text = await response.text();
        throw new Error(`Amazon Places ${endpoint} ${response.status}: ${text.slice(0, 150)}`);
    }
    return response.json();
}

// ─── LOAD PRODUCTS FROM BACKEND API ───
export async function loadProductsFromAPI() {
    try {
        const response = await fetch(`${API_BASE}/api/products`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        if (data && data.length > 0) {
            state.products = data;
            state.categories = [...new Set(state.products.map(p => p.tag))].sort();
            state.isDataLoaded = true;
            console.log(`✅ Loaded ${state.products.length} products from backend API`);
            console.log('📦 Categories:', state.categories);
            return true;
        } else {
            console.warn('⚠️ No products available from database');
            return false;
        }
    } catch (err) {
        console.warn('⚠️ API fetch failed:', err);
        return false;
    }
}

// ─── LOAD BANNERS ───
export async function loadBannersFromAPI() {
    try {
        const response = await fetch(`${API_BASE}/api/v1/banners/active`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        if (data && data.length > 0) {
            state.banners = data;
            console.log(`✅ Loaded ${state.banners.length} banners from backend API`);
            renderHeroSlides();
            return true;
        } else {
            console.warn('⚠️ No active banners in database');
            return false;
        }
    } catch (err) {
        console.warn('⚠️ Banner API fetch failed:', err);
        return false;
    }
}

// ─── Check delivery coverage with full intelligent fee ───
export async function checkDeliveryCoverage(address, coords = null, attempt = 0) {
    if (!address || address.trim().length < 5) {
        return { covered: false, error: 'Please enter a full address' };
    }

    if (attempt === 0) {
        state.isCheckingDelivery = true;
        updateDeliveryStatusLoading('Checking delivery availability...');
    }

    const items = Object.values(state.cart).map(item => ({
        product_id: parseInt(item.id) || 0,
        qty: parseInt(item.qty) || 1,
        name: item.name || 'Unknown',
        price: parseInt(item.price) || 0
    }));
    const email = dom.customerEmail.value.trim();

    try {
        const payload = {
            address: address.trim(),
            items: items.map(item => ({
                name: item.name || 'Unknown',
                qty: item.qty || 1,
                price: item.price || 0,
                product_id: parseInt(item.product_id) || 0
            })),
            order_total: state.cartTotalPrice || 0,
            customer_email: email || undefined
        };
        if (coords && typeof coords.lat === 'number' && typeof coords.lng === 'number') {
            payload.lat = coords.lat;
            payload.lng = coords.lng;
        }

        const response = await fetch(`${API_BASE}/api/delivery-fee`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        // 4xx = a real answer ("not covered" / "bad address") — do not retry.
        if (response.status >= 400 && response.status < 500) {
            let msg = 'Delivery is not available for this address.';
            try {
                const errData = await response.json();
                msg = errData.message || errData.detail || msg;
            } catch (_) { /* ignore */ }
            return { covered: false, error: msg };
        }

        // 5xx = server problem — fall through to retry
        if (!response.ok) throw new Error(`Server error ${response.status}`);

        return await response.json();

    } catch (err) {
        if (attempt < DELIVERY_FEE_MAX_RETRIES) {
            console.warn(`Delivery fee attempt ${attempt + 1} failed, retrying…`, err.message);
            await new Promise(r => setTimeout(r, DELIVERY_FEE_RETRY_DELAY_MS));
            return checkDeliveryCoverage(address, coords, attempt + 1);
        }

        // Final failure — fail closed. Never invent a fee.
        console.error('Delivery pricing unavailable:', err);
        return {
            covered: false,
            unavailable: true,
            error: err.message || 'Delivery pricing service unavailable'
        };
    } finally {
        if (attempt === 0) state.isCheckingDelivery = false;
    }
}

// Small helper used above (kept local to avoid a circular import with delivery.js)
function updateDeliveryStatusLoading(message) {
    dom.deliveryStatus.className = 'delivery-status loading';
    dom.deliveryStatus.textContent = message;
}

// ─── FETCH ORDER BY REFERENCE ───
// Requires the customer email — /api/orders/by-reference/{ref} needs
// ?email=<customer_email> to prevent reference-only PII leaks.
export async function fetchOrderByReference(ref, email) {
    if (!ref || !email) return null;
    try {
        const url = `${API_BASE}/api/orders/by-reference/${encodeURIComponent(ref)}?email=${encodeURIComponent(email)}`;
        const response = await fetch(url);
        if (response.status === 404) return null;
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return await response.json();
    } catch (err) {
        console.warn('Failed to fetch order:', err);
        return null;
    }
}

// ─── ORDER LOOKUP (track order modal) ───
export async function lookupOrder(ref, email) {
    try {
        const url = `${API_BASE}/api/orders/by-reference/${encodeURIComponent(ref)}?email=${encodeURIComponent(email)}`;
        const response = await fetch(url);
        if (response.status === 404) return { notFound: true };
        if (!response.ok) {
            const errText = await response.text().catch(function() { return ''; });
            throw new Error('HTTP ' + response.status + ' ' + errText.slice(0, 100));
        }
        return await response.json();
    } catch (err) {
        console.warn('Order lookup failed:', err);
        return { error: err.message };
    }
}
