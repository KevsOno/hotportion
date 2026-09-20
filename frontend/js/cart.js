import { state } from './state.js';
import { dom } from './dom.js';
import { PERSIST_KEY, PERSIST_MAX_AGE_MS, CUSTOMER_EMAIL_KEY, CHECKOUT_IDEMPOTENCY_KEY } from './config.js';
import { formatPrice, showToast } from './utils.js';
import { updateCheckoutButton } from './checkout.js';
import { handleDeliveryAddressChange } from './delivery.js';

// ================================================================
// ─── PERSISTENCE (localStorage) ───
// ================================================================

export function persistState() {
    try {
        const snapshot = {
            cart: state.cart,
            customer_name: dom.customerName.value,
            customer_email: dom.customerEmail.value,
            customer_phone: dom.customerPhone.value,
            delivery_method: state.deliveryMethod,
            payment_method: state.paymentMethod,
            delivery_address: dom.deliveryAddress.value,
            preferred_time: dom.preferredTime.value,
            order_notes: dom.orderNotes.value,
            saved_at: Date.now()
        };
        localStorage.setItem(PERSIST_KEY, JSON.stringify(snapshot));
    } catch (e) {
        console.warn('persistState failed:', e);
    }
}

export function restoreState() {
    try {
        const raw = localStorage.getItem(PERSIST_KEY);
        if (!raw) return;
        const saved = JSON.parse(raw);

        if (saved.saved_at && (Date.now() - saved.saved_at) > PERSIST_MAX_AGE_MS) {
            localStorage.removeItem(PERSIST_KEY);
            return;
        }

        if (saved.cart && typeof saved.cart === 'object' && !Array.isArray(saved.cart)) {
            state.cart = saved.cart;
        }
        if (saved.customer_name) dom.customerName.value = saved.customer_name;
        if (saved.customer_email) dom.customerEmail.value = saved.customer_email;
        if (saved.customer_phone) dom.customerPhone.value = saved.customer_phone;
        if (saved.delivery_address) dom.deliveryAddress.value = saved.delivery_address;
        if (saved.preferred_time) dom.preferredTime.value = saved.preferred_time;
        if (saved.order_notes) dom.orderNotes.value = saved.order_notes;
        if (saved.delivery_method && ['pickup', 'delivery', 'dinein'].indexOf(saved.delivery_method) !== -1) {
            state._restoredDeliveryMethod = saved.delivery_method;
        }
        if (saved.payment_method && ['online', 'offline'].indexOf(saved.payment_method) !== -1) {
            state._restoredPaymentMethod = saved.payment_method;
        }

        console.log('✅ State restored from localStorage:', Object.keys(state.cart).length, 'cart item(s)');
    } catch (e) {
        console.warn('restoreState failed:', e);
    }
}

export function clearPersistedState() {
    try {
        localStorage.removeItem(PERSIST_KEY);
        console.log('🧹 Persisted state cleared');
    } catch (e) { /* ignore */ }
}

// ─── Customer email persistence ───
export function saveCustomerEmail(email) {
    try {
        if (email && String(email).trim()) {
            localStorage.setItem(CUSTOMER_EMAIL_KEY, String(email).trim().toLowerCase());
        }
    } catch (e) { /* ignore */ }
}

export function getSavedCustomerEmail() {
    try {
        return localStorage.getItem(CUSTOMER_EMAIL_KEY) || '';
    } catch (e) { return ''; }
}

// ─── CART OPERATIONS ───

export function addToCart(productId, qty = 1) {
    const product = state.products.find(p => p.id == productId);
    if (!product) return;
    if (state.cart[productId]) {
        state.cart[productId].qty += qty;
    } else {
        state.cart[productId] = { ...product, qty: qty };
    }
    updateCartUI();
    persistState();
    if (state.deliveryMethod === 'delivery' && dom.deliveryAddress.value.trim().length >= 5) {
        state.lastCheckedAddress = '';
        setTimeout(handleDeliveryAddressChange, 100);
    }
    showToast(`${product.name} added to cart!`, 'fa-solid fa-check-circle');
}

export function removeFromCart(productId) {
    if (state.cart[productId]) {
        delete state.cart[productId];
        updateCartUI();
        persistState();
        if (state.deliveryMethod === 'delivery' && dom.deliveryAddress.value.trim().length >= 5) {
            state.lastCheckedAddress = '';
            setTimeout(handleDeliveryAddressChange, 100);
        }
    }
}

export function updateQty(productId, delta) {
    if (!state.cart[productId]) return;
    const newQty = state.cart[productId].qty + delta;
    if (newQty <= 0) {
        removeFromCart(productId);
    } else {
        state.cart[productId].qty = newQty;
        updateCartUI();
        persistState();
        if (state.deliveryMethod === 'delivery' && dom.deliveryAddress.value.trim().length >= 5) {
            state.lastCheckedAddress = '';
            setTimeout(handleDeliveryAddressChange, 100);
        }
    }
}

export function updateCartUI() {
    let totalItems = 0;
    let totalPrice = 0;
    const items = Object.values(state.cart);
    items.forEach(item => {
        totalItems += item.qty;
        totalPrice += item.price * item.qty;
    });
    state.cartTotalItems = totalItems;
    state.cartTotalPrice = totalPrice;
    dom.cartBadge.textContent = totalItems;
    if (dom.navCartBadge) dom.navCartBadge.textContent = totalItems;

    if (totalItems === 0) {
        dom.cartItemsEl.innerHTML = `
            <div class="cart-empty">
                <i class="fa-regular fa-bag-shopping"></i>
                <p>Your cart is empty.<br />Add some delicious items!</p>
            </div>
        `;
    } else {
        let html = '';
        items.forEach(item => {
            const itemTotal = item.price * item.qty;
            const imageHtml = item.image ?
                `<img src="${item.image}" alt="${item.name}" class="item-image" loading="lazy" width="48" height="48" data-hide-on-error>` :
                `<span class="item-emoji">${item.emoji || '🍽️'}</span>`;
            html += `
                <div class="cart-item" data-id="${item.id}">
                    ${imageHtml}
                    <div class="item-info">
                        <h4>${item.name}</h4>
                        <div class="item-price">${formatPrice(item.price)} each</div>
                    </div>
                    <div class="item-qty">
                        <button class="qty-minus" data-id="${item.id}">−</button>
                        <span class="qty-num">${item.qty}</span>
                        <button class="qty-plus" data-id="${item.id}">+</button>
                        <button class="remove" data-id="${item.id}" style="color:var(--primary);font-size:0.9rem;width:28px;height:28px;border:none;background:transparent;cursor:pointer;border-radius:50%;display:flex;align-items:center;justify-content:center;"><i class="fa-regular fa-trash-can"></i></button>
                    </div>
                    <div class="item-total">${formatPrice(itemTotal)}</div>
                </div>
            `;
        });
        dom.cartItemsEl.innerHTML = html;
        dom.cartItemsEl.querySelectorAll('.qty-minus').forEach(btn => {
            btn.addEventListener('click', function() {
                const id = this.dataset.id;
                updateQty(id, -1);
            });
        });
        dom.cartItemsEl.querySelectorAll('.qty-plus').forEach(btn => {
            btn.addEventListener('click', function() {
                const id = this.dataset.id;
                updateQty(id, 1);
            });
        });
        dom.cartItemsEl.querySelectorAll('.remove').forEach(btn => {
            btn.addEventListener('click', function() {
                const id = this.dataset.id;
                removeFromCart(id);
            });
        });
    }

    const subtotal = state.cartTotalPrice;
    const finalDeliveryFee = (state.deliveryMethod === 'delivery' && state.isDeliveryCovered) ? state.deliveryFee : 0;
    const total = subtotal + finalDeliveryFee;
    dom.cartSubtotal.textContent = formatPrice(subtotal);
    dom.cartDeliveryFee.textContent = formatPrice(finalDeliveryFee);
    if (state.deliveryMethod === 'delivery' && finalDeliveryFee > 0) {
        dom.deliveryFeeRow.style.display = 'flex';
    } else {
        dom.deliveryFeeRow.style.display = 'none';
    }
    dom.cartTotal.textContent = formatPrice(total);

    updateCheckoutButton();
    // Dynamic images may have been re-rendered — rebind fallbacks.
    import('./utils.js').then(({ bindImageFallbacks }) => bindImageFallbacks()).catch(() => {});
}

// Restore immediately (DOM refs exist by this point)
restoreState();
