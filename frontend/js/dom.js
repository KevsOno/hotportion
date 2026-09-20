// ─── Central registry of DOM references ───
function $ (id) { return document.getElementById(id); }

export const dom = {
    // Menu / cart
    menuGrid: $('menu-grid'),
    shimmerWrapper: $('shimmer-wrapper'),
    cartItemsEl: $('cart-items'),
    cartBadge: $('cart-badge'),
    navCartBadge: $('nav-cart-badge'),
    cartSubtotal: $('cart-subtotal'),
    cartDeliveryFee: $('cart-delivery-fee'),
    cartTotal: $('cart-total'),
    cartDrawer: $('cart-drawer'),
    cartOverlay: $('cart-overlay'),
    cartToggle: $('cart-toggle'),
    cartClose: $('cart-close'),
    toast: $('toast'),
    toastMsg: $('toast-msg'),
    orderNowBtn: $('order-now-btn'),
    heroOrderBtn: $('hero-order-btn'),
    heroMenuBtn: $('hero-menu-btn'),
    footerCart: $('footer-cart'),
    checkoutBtn: $('checkout-btn'),
    featuredGrid: $('featured-grid'),
    searchInput: $('search-input'),
    searchClear: $('search-clear'),
    searchBtn: $('search-btn'),
    categoriesPills: $('categories-pills'),

    // Delivery / payment options
    deliveryOptions: document.querySelectorAll('#delivery-options button'),
    paymentMethodGroup: $('payment-method-group'),
    paymentOptions: document.querySelectorAll('#payment-options button'),
    paymentMethodHint: $('payment-method-hint'),
    cartFooterNote: $('cart-footer-note'),

    // Address
    addressGroup: $('address-group'),
    deliveryAddress: $('delivery-address'),
    deliveryFeeDisplay: $('delivery-fee-display'),
    deliveryStatus: $('delivery-status'),
    gpsBtn: $('gps-btn'),
    customerName: $('customer-name'),
    customerEmail: $('customer-email'),
    customerPhone: $('customer-phone'),
    preferredTime: $('preferred-time'),
    orderNotes: $('order-notes'),
    deliveryFeeRow: $('delivery-fee-row'),
    feeBreakdownToggle: $('fee-breakdown-toggle'),
    deliveryBreakdown: $('delivery-breakdown'),
    addressDropdown: $('address-autocomplete-dropdown'),

    // Breakdown values
    breakdownBase: $('breakdown-base'),
    breakdownDiscount: $('breakdown-discount'),
    breakdownSurcharge: $('breakdown-surcharge'),
    breakdownPeak: $('breakdown-peak'),
    breakdownLoyalty: $('breakdown-loyalty'),
    breakdownTotal: $('breakdown-total'),

    // Receipt
    receiptOverlay: $('receipt-overlay'),
    receiptModal: $('receipt-modal'),
    receiptBody: $('receipt-body'),
    receiptClose: $('receipt-close'),
    receiptStatusIcon: $('receipt-status-icon'),
    receiptStatusTitle: $('receipt-status-title'),
    receiptStatusSubtitle: $('receipt-status-subtitle'),

    // Chat
    chatModal: $('chat-modal'),
    chatMessages: $('chat-messages'),
    chatInput: $('chat-input'),
    chatSend: $('chat-send'),
    chatBubble: $('chat-bubble'),
    closeChat: $('close-chat-modal'),
    chatLauncher: $('chat-launcher'),

    // Bottom nav
    navHome: $('nav-home'),
    navMenu: $('nav-menu'),
    navChat: $('nav-chat'),
    navCartBottom: $('nav-cart-bottom'),

    // Track order
    trackOverlay: $('track-overlay'),
    trackCloseBtn: $('track-close'),
    trackRefInput: $('track-ref-input'),
    trackEmailInput: $('track-email-input'),
    trackSubmit: $('track-submit'),
    trackResult: $('track-result'),
    footerTrack: $('footer-track'),
};
