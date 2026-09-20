// ─── API BASE: Capacitor native vs Web ───
export function getApiBase() {
    if (window.Capacitor && window.Capacitor.isNative) {
        return 'https://hotportion.onrender.com';
    }
    return '';
}

export const API_BASE = getApiBase();
console.log('🔗 API_BASE:', API_BASE || '(web proxy)');

export const CHECKOUT_IDEMPOTENCY_KEY = 'hp_checkout_idempotency';
export const PERSIST_KEY = 'hp_state';
export const PERSIST_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000; // 7 days
export const CUSTOMER_EMAIL_KEY = 'hp_customer_email';
export const DELIVERY_FEE_MAX_RETRIES = 1;
export const DELIVERY_FEE_RETRY_DELAY_MS = 800;
