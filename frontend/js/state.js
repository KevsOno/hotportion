// ─── Shared mutable application state ───
export const state = {
    products: [],
    categories: [],
    banners: [],
    cart: {},
    cartTotalItems: 0,
    cartTotalPrice: 0,
    searchQuery: '',
    selectedCategory: '',
    deliveryMethod: 'pickup',
    deliveryFee: 0,
    lastOrderData: null,
    isDataLoaded: false,

    // [FEATURE] Payment method state. 'online' = Monnify checkout;
    // 'offline' = pay at counter on arrival (pickup/dine-in only).
    paymentMethod: 'online',

    // [CHANGE] Checkout idempotency key. Generated on the first click of
    // "Proceed to Payment", reused on any retry of that same attempt,
    // and cleared when the receipt is closed.
    checkoutIdempotencyKey: null,

    // ─── Delivery Coverage State ───
    isDeliveryCovered: false,
    isDeliveryAvailable: true,   // false = pricing API unreachable
    isCheckingDelivery: false,
    lastCheckedAddress: '',
    lastBreakdown: null,

    // ─── Persisted delivery method restored from localStorage ───
    _restoredDeliveryMethod: 'pickup',
    _restoredPaymentMethod: 'online',
};

// Load the persisted idempotency key (if any) at startup.
try {
    state.checkoutIdempotencyKey = sessionStorage.getItem('hp_checkout_idempotency') || null;
} catch (e) { /* ignore */ }
