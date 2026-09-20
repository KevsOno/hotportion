import { API_BASE, CHECKOUT_IDEMPOTENCY_KEY } from './config.js';
import { state } from './state.js';
import { dom } from './dom.js';
import { showToast } from './utils.js';
import { fetchOrderByReference } from './api.js';
import { updateCartUI, persistState, saveCustomerEmail, getSavedCustomerEmail, clearPersistedState } from './cart.js';
import { handleDeliveryAddressChange, resetDeliveryStatus, updateDeliveryFeeUI } from './delivery.js';
import { showReceipt } from './receipt.js';
import { refreshProducts } from './menu.js';
import { closeCart } from './ui.js';

// ─── UPDATE CHECKOUT BUTTON ───
export function updateCheckoutButton() {
    const items = Object.values(state.cart);
    const hasItems = items.length > 0;
    const name = dom.customerName.value.trim();
    const email = dom.customerEmail.value.trim();
    const phone = dom.customerPhone.value.trim();
    const isDelivery = state.deliveryMethod === 'delivery';
    const addressOk = !isDelivery || (state.isDeliveryCovered && dom.deliveryAddress.value.trim().length > 5);
    const isOffline = (state.paymentMethod === 'offline' && !isDelivery);

    let disabled = !hasItems || !name || !email || !phone;
    if (isDelivery) {
        disabled = disabled || !addressOk;
    }

    dom.checkoutBtn.disabled = disabled;

    if (!hasItems) {
        dom.checkoutBtn.innerHTML = '<i class="fa-regular fa-credit-card"></i> Add items to cart';
    } else if (isDelivery && !state.isDeliveryAvailable && dom.deliveryAddress.value.trim().length > 5) {
        dom.checkoutBtn.innerHTML = '<i class="fa-solid fa-triangle-exclamation"></i> Delivery pricing unavailable';
    } else if (isDelivery && !state.isDeliveryCovered && dom.deliveryAddress.value.trim().length > 5) {
        dom.checkoutBtn.innerHTML = '<i class="fa-solid fa-triangle-exclamation"></i> Address not covered';
    } else if (isDelivery && dom.deliveryAddress.value.trim().length < 5) {
        dom.checkoutBtn.innerHTML = '<i class="fa-regular fa-credit-card"></i> Enter delivery address';
    } else if (!name || !email || !phone) {
        dom.checkoutBtn.innerHTML = '<i class="fa-regular fa-credit-card"></i> Fill in your details';
    } else if (isOffline) {
        dom.checkoutBtn.innerHTML = '<i class="fa-solid fa-bag-shopping"></i> Place Order';
    } else {
        dom.checkoutBtn.innerHTML = '<i class="fa-regular fa-credit-card"></i> Proceed to Payment';
    }
}

// ─── PAYMENT METHOD ───
export function setPaymentMethod(method) {
    state.paymentMethod = (method === 'offline') ? 'offline' : 'online';
    dom.paymentOptions.forEach(btn => {
        btn.classList.toggle('active', btn.dataset.payment === state.paymentMethod);
    });
    if (dom.paymentMethodHint) {
        dom.paymentMethodHint.textContent = state.paymentMethod === 'offline'
            ? 'Pay at the counter when you arrive. No online payment needed.'
            : 'Secure online payment with Monnify.';
    }
    updateCartFooterNote();
    updateCheckoutButton();
    persistState();
}

// ─── Cart footer note reflects the current payment method. ───
export function updateCartFooterNote() {
    if (!dom.cartFooterNote) return;
    if (state.paymentMethod === 'offline' && state.deliveryMethod !== 'delivery') {
        dom.cartFooterNote.innerHTML = '💵 Pay at the counter when you arrive';
    } else {
        dom.cartFooterNote.innerHTML = '🔒 Secure payment with Monnify';
    }
}

// ─── DELIVERY METHOD ───
export function setDeliveryMethod(method) {
    state.deliveryMethod = method;
    dom.deliveryOptions.forEach(btn => {
        btn.classList.toggle('active', btn.dataset.method === method);
    });

    if (method === 'delivery') {
        if (dom.paymentMethodGroup) dom.paymentMethodGroup.style.display = 'none';
        if (state.paymentMethod !== 'online') {
            state.paymentMethod = 'online';
            dom.paymentOptions.forEach(b => b.classList.toggle('active', b.dataset.payment === 'online'));
            if (dom.paymentMethodHint) {
                dom.paymentMethodHint.textContent = 'Secure online payment with Monnify.';
            }
        }

        state.isDeliveryAvailable = true;
        dom.addressGroup.style.display = 'block';
        dom.deliveryAddress.required = true;
        if (dom.deliveryAddress.value.trim().length >= 5) {
            handleDeliveryAddressChange();
        } else {
            resetDeliveryStatus();
            state.deliveryFee = 0;
            updateDeliveryFeeUI(0);
        }
    } else {
        if (dom.paymentMethodGroup) dom.paymentMethodGroup.style.display = 'block';
        dom.addressGroup.style.display = 'none';
        dom.deliveryAddress.required = false;
        state.isDeliveryCovered = false;
        state.deliveryFee = 0;
        state.lastBreakdown = null;
        resetDeliveryStatus();
        updateDeliveryFeeUI(0);
        dom.deliveryFeeRow.style.display = 'none';
        dom.deliveryBreakdown.classList.remove('show');
    }

    updateCartFooterNote();
    updateCartUI();
    updateCheckoutButton();
    persistState();
}

// ─── HANDLE MONNIFY REDIRECT ───
export function handlePaymentRedirect() {
    let queryString = window.location.search;
    if (queryString.includes('?') && queryString.indexOf('?') !== queryString.lastIndexOf('?')) {
        queryString = '?' + window.location.search.split('?').pop();
    }
    const urlParams = new URLSearchParams(queryString);
    const transactionRef = urlParams.get('transactionReference') || urlParams.get('paymentReference');
    let status = urlParams.get('status') || urlParams.get('paymentStatus');

    if (transactionRef) {
        window.history.replaceState({}, document.title, window.location.pathname);

        let orderData = null;
        const stored = sessionStorage.getItem('pending_order');
        if (stored) {
            try {
                orderData = JSON.parse(stored);
            } catch (e) { /* ignore */ }
        }

        const normalizedStatus = (status || '').toUpperCase();
        const paymentOk = normalizedStatus === 'PAID' || normalizedStatus === 'SUCCESS';

        if (!orderData) {
            const savedEmail = getSavedCustomerEmail();
            if (!savedEmail) {
                showToast('Please open your order confirmation email for details.', 'fa-solid fa-exclamation-circle');
                sessionStorage.removeItem('pending_order');
                return;
            }
            fetchOrderByReference(transactionRef, savedEmail)
                .then(data => {
                    if (data) {
                        if (paymentOk) {
                            showReceipt(data);
                            refreshProducts();
                        } else {
                            showToast('Payment failed or was cancelled. Please try again.', 'fa-solid fa-exclamation-circle');
                        }
                    } else {
                        showToast('Unable to retrieve order details. Please contact support.', 'fa-solid fa-exclamation-circle');
                    }
                    sessionStorage.removeItem('pending_order');
                })
                .catch(() => {
                    showToast('Error processing payment. Please contact support.', 'fa-solid fa-exclamation-circle');
                    sessionStorage.removeItem('pending_order');
                });
            return;
        }

        if (paymentOk) {
            showReceipt(orderData);
            refreshProducts();
        } else {
            showToast('Payment failed or was cancelled. Please try again.', 'fa-solid fa-exclamation-circle');
        }
        sessionStorage.removeItem('pending_order');
    }
}

// ─── CHECKOUT ───
export async function checkout() {
    const items = Object.values(state.cart);
    if (items.length === 0) {
        showToast('Your cart is empty!', 'fa-solid fa-exclamation-circle');
        return;
    }
    const name = dom.customerName.value.trim();
    const email = dom.customerEmail.value.trim();
    const phone = dom.customerPhone.value.trim();
    if (!name || !email || !phone) {
        showToast('Please enter your name, email, and phone number.', 'fa-solid fa-exclamation-circle');
        return;
    }
    if (state.deliveryMethod === 'delivery') {
        if (!dom.deliveryAddress.value.trim()) {
            showToast('Please enter your delivery address.', 'fa-solid fa-exclamation-circle');
            return;
        }
        if (!state.isDeliveryCovered) {
            showToast('We don\'t deliver to this area. Please choose Pickup or Dine-in.', 'fa-solid fa-exclamation-circle');
            return;
        }
    }

    // Generate the idempotency key once per checkout attempt.
    if (!state.checkoutIdempotencyKey) {
        state.checkoutIdempotencyKey = (window.crypto && typeof window.crypto.randomUUID === 'function')
            ? window.crypto.randomUUID()
            : ('idem-' + Date.now() + '-' + Math.random().toString(36).slice(2, 12));
        try { sessionStorage.setItem(CHECKOUT_IDEMPOTENCY_KEY, state.checkoutIdempotencyKey); } catch (e) { /* ignore */ }
    }

    saveCustomerEmail(email);

    const effectivePaymentMethod = (state.paymentMethod === 'offline' && state.deliveryMethod !== 'delivery')
        ? 'offline'
        : 'online';

    const subtotal = state.cartTotalPrice;
    const finalDeliveryFee = (state.deliveryMethod === 'delivery' && state.isDeliveryCovered) ? state.deliveryFee : 0;
    const total = subtotal + finalDeliveryFee;

    const orderPayload = {
        customer_name: name,
        customer_email: email,
        customer_phone: phone,
        total: total,
        status: 'pending',
        delivery_method: state.deliveryMethod,
        payment_method: effectivePaymentMethod,
        delivery_address: state.deliveryMethod === 'delivery' ? dom.deliveryAddress.value : '',
        preferred_time: dom.preferredTime.value || '',
        order_notes: dom.orderNotes.value || '',
        delivery_fee: finalDeliveryFee,
        items: items.map(i => ({ name: i.name, qty: i.qty, price: i.price, product_id: i.id })),
        idempotency_key: state.checkoutIdempotencyKey,
    };

    if (state.lastBreakdown && state.deliveryMethod === 'delivery') {
        orderPayload.delivery_breakdown = state.lastBreakdown;
    }

    const btn = dom.checkoutBtn;
    const originalText = btn.innerHTML;
    btn.innerHTML = '<span class="loader-small"></span> ' + (effectivePaymentMethod === 'offline' ? 'Placing order...' : 'Redirecting to payment...');
    btn.disabled = true;

    try {
        const response = await fetch(`${API_BASE}/api/orders`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(orderPayload)
        });

        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.message || err.detail || 'Order creation failed');
        }

        const result = await response.json();
        console.log('✅ Order created:', result);

        if (result.payment_method === 'offline' || result.status === 'awaiting_payment') {
            const offlineReceipt = {
                payment_reference: result.payment_reference,
                order_id: result.order_id,
                customer_name: name,
                customer_email: email,
                customer_phone: phone,
                items: orderPayload.items,
                total: total,
                delivery_method: state.deliveryMethod,
                payment_method: 'offline',
                delivery_address: null,
                delivery_fee: 0,
                preferred_time: dom.preferredTime.value || '',
                status: 'awaiting_payment',
                created_at: new Date().toISOString()
            };
            showReceipt(offlineReceipt);
            return;
        }

        sessionStorage.setItem('pending_order', JSON.stringify({ ...result, customer_email: email }));

        if (result.checkout_url) {
            window.location.href = result.checkout_url;
        } else {
            showToast('Payment init failed. Please try again.', 'fa-solid fa-exclamation-circle');
            btn.innerHTML = originalText;
            btn.disabled = false;
        }

    } catch (err) {
        console.error('🔥 Checkout error:', err);
        showToast('Error: ' + err.message, 'fa-solid fa-exclamation-circle');
        btn.innerHTML = originalText;
        btn.disabled = false;
    }
}
