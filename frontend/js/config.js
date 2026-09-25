// ─── API BASE: Capacitor native vs Web ───
export function getApiBase() {
    // Capacitor 5+: isNativePlatform() is a method, not a property.
    // Also detect the WebView origin directly as a fallback, since
    // window.Capacitor may not be injected before this module runs.
    const cap = window.Capacitor;
    const isNative = !!(cap && typeof cap.isNativePlatform === 'function' && cap.isNativePlatform());

    const origin = window.location.origin || '';
    const isLocalhostOrigin =
        origin === 'https://localhost' ||
        origin === 'capacitor://localhost' ||
        origin === 'http://localhost';

    if (isNative || isLocalhostOrigin) {
        return 'https://hotportion.onrender.com';
    }
    return ''; // Web (Netlify) — use the proxy redirect
}

export const API_BASE = getApiBase();
console.log('🔗 API_BASE:', API_BASE || '(web proxy)');

export const CHECKOUT_IDEMPOTENCY_KEY = 'hp_checkout_idempotency';
export const PERSIST_KEY = 'hp_state';
export const PERSIST_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000; // 7 days
export const CUSTOMER_EMAIL_KEY = 'hp_customer_email';
export const DELIVERY_FEE_MAX_RETRIES = 1;
export const DELIVERY_FEE_RETRY_DELAY_MS = 800;
