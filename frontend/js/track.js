import { state } from './state.js';
import { dom } from './dom.js';
import { escapeHtml } from './utils.js';
import { lookupOrder } from './api.js';
import { saveCustomerEmail, getSavedCustomerEmail } from './cart.js';

export function openTrackModal(prefillRef) {
    if (!dom.trackOverlay) return;
    dom.trackOverlay.classList.add('open');
    document.body.style.overflow = 'hidden';
    if (prefillRef) dom.trackRefInput.value = prefillRef;
    if (dom.trackEmailInput && !dom.trackEmailInput.value) {
        const savedEmail = getSavedCustomerEmail();
        if (savedEmail) dom.trackEmailInput.value = savedEmail;
    }
    setTimeout(function() {
        if (!dom.trackRefInput.value) {
            dom.trackRefInput.focus();
        } else if (dom.trackEmailInput && !dom.trackEmailInput.value) {
            dom.trackEmailInput.focus();
        } else {
            dom.trackSubmit.focus();
        }
    }, 200);
}

export function closeTrackModal() {
    if (!dom.trackOverlay) return;
    dom.trackOverlay.classList.remove('open');
    document.body.style.overflow = '';
}

export function renderTrackResult(order) {
    if (!order) {
        dom.trackResult.className = 'track-result error';
        dom.trackResult.textContent = 'Order not found. Please check the reference and try again.';
        return;
    }
    const status = (order.status || 'pending').toLowerCase();
    const statusLabels = {
        pending: '⏳ Awaiting Payment',
        awaiting_payment: '💵 Awaiting Counter Payment',
        paid: '✅ Payment Received',
        confirmed: '👨‍🍳 Preparing Your Order',
        completed: '🎉 Completed',
        cancelled: '❌ Cancelled'
    };
    const statusLabel = statusLabels[status] || status.toUpperCase();
    const itemsHtml = (order.items || []).map(function(item) {
        return '<div class="item-row">' +
            '<span class="name">' + escapeHtml(item.name || 'Item') + ' <small>×' + (item.qty || 0) + '</small></span>' +
            '<span class="price">₦' + Number((item.price || 0) * (item.qty || 0)).toLocaleString() + '</span>' +
            '</div>';
    }).join('');
    const dm = { pickup: '🏃 Pickup', delivery: '🛵 Delivery', dinein: '🍽️ Dine-in' }[order.delivery_method] || 'Pickup';
    const created = order.created_at ? new Date(order.created_at).toLocaleString() : 'N/A';
    const ref = order.payment_reference || '—';
    const deliveryHtml = (order.delivery_method === 'delivery' && order.delivery_address) ?
        '<div class="row"><span class="label">Delivery to</span><span class="value">' + escapeHtml(order.delivery_address) + '</span></div>' +
        '<div class="row"><span class="label">Delivery Fee</span><span class="value">₦' + Number(order.delivery_fee || 0).toLocaleString() + '</span></div>'
        : '';

    dom.trackResult.className = 'track-result';
    dom.trackResult.innerHTML =
        '<div class="order-status-card">' +
        '<span class="order-status-badge ' + status + '">' + statusLabel + '</span>' +
        '<div class="row"><span class="label">Reference</span><span class="value" style="font-family:monospace;font-size:0.78rem;">' + escapeHtml(ref) + '</span></div>' +
        '<div class="row"><span class="label">Placed</span><span class="value">' + created + '</span></div>' +
        '<div class="row"><span class="label">Method</span><span class="value">' + dm + '</span></div>' +
        deliveryHtml +
        (itemsHtml ? '<div class="order-items-mini"><h5>Items</h5>' + itemsHtml + '</div>' : '') +
        '<div class="total-row"><span>Total</span><span>₦' + Number(order.total || 0).toLocaleString() + '</span></div>' +
        '</div>';
}

export async function handleTrackSubmit() {
    const ref = (dom.trackRefInput.value || '').trim();
    const email = (dom.trackEmailInput ? (dom.trackEmailInput.value || '') : '').trim();
    if (!ref) {
        dom.trackResult.className = 'track-result error';
        dom.trackResult.textContent = 'Please enter an order reference.';
        dom.trackRefInput.focus();
        return;
    }
    if (!email) {
        dom.trackResult.className = 'track-result error';
        dom.trackResult.textContent = 'Please enter the email you used at checkout.';
        if (dom.trackEmailInput) dom.trackEmailInput.focus();
        return;
    }
    saveCustomerEmail(email);

    dom.trackSubmit.disabled = true;
    const originalHtml = dom.trackSubmit.innerHTML;
    dom.trackSubmit.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Searching...';
    dom.trackResult.className = 'track-result loading';
    dom.trackResult.textContent = 'Looking up your order...';

    const result = await lookupOrder(ref, email);

    dom.trackSubmit.disabled = false;
    dom.trackSubmit.innerHTML = originalHtml;

    if (result && result.notFound) {
        dom.trackResult.className = 'track-result error';
        dom.trackResult.textContent = 'No order found for that reference and email. Please check and try again.';
        return;
    }
    if (result && result.error) {
        dom.trackResult.className = 'track-result error';
        dom.trackResult.textContent = 'Lookup failed: ' + result.error;
        return;
    }
    renderTrackResult(result);
}
