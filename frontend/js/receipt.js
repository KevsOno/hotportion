import { state } from './state.js';
import { dom } from './dom.js';
import { escapeHtml, formatPrice, showToast } from './utils.js';
import { updateCartUI, clearPersistedState } from './cart.js';
import { resetDeliveryStatus, updateDeliveryFeeUI } from './delivery.js';
import { refreshProducts } from './menu.js';
import { closeCart } from './ui.js';
import { CHECKOUT_IDEMPOTENCY_KEY } from './config.js';

// ─── RECEIPT ───
export function showReceipt(orderData) {
    state.lastOrderData = orderData;
    const isOffline = (orderData.payment_method === 'offline');

    const items = orderData.items || [];
    const deliveryMethodLabel = {
        pickup: '🏃 Pickup',
        delivery: '🛵 Delivery',
        dinein: '🍽️ Dine-in'
    } [orderData.delivery_method] || 'Pickup';

    if (isOffline) {
        if (dom.receiptStatusIcon) dom.receiptStatusIcon.className = 'fa-solid fa-money-bill-wave';
        if (dom.receiptStatusTitle) dom.receiptStatusTitle.textContent = 'Order Placed!';
        if (dom.receiptStatusSubtitle) dom.receiptStatusSubtitle.textContent =
            `Pay ${formatPrice(orderData.total)} at the counter when you arrive.`;
    } else {
        if (dom.receiptStatusIcon) dom.receiptStatusIcon.className = 'fa-regular fa-circle-check';
        if (dom.receiptStatusTitle) dom.receiptStatusTitle.textContent = 'Payment Successful!';
        if (dom.receiptStatusSubtitle) dom.receiptStatusSubtitle.textContent = 'Your order has been confirmed.';
    }

    let itemsHtml = items.map(item => `
        <div class="receipt-item-row">
            <span class="item-name">${escapeHtml(item.name)} <span class="qty-badge">×${item.qty}</span></span>
            <span>${formatPrice(item.price * item.qty)}</span>
        </div>
    `).join('');

    let deliveryHtml = '';
    if (orderData.delivery_method === 'delivery') {
        deliveryHtml = `
            <div class="receipt-row">
                <span class="label">Delivery Fee</span>
                <span class="value">${formatPrice(orderData.delivery_fee || 0)}</span>
            </div>
            <div class="receipt-row">
                <span class="label">Delivery Address</span>
                <span class="value" style="font-weight:400;max-width:60%;">${escapeHtml(orderData.delivery_address) || 'N/A'}</span>
            </div>
        `;
    }

    const totalLabel = isOffline ? 'Total Due' : 'Total Paid';

    const footerHtml = isOffline
        ? `<div class="receipt-footer">
               <i class="fa-solid fa-money-bill-wave"></i> Show this reference at the counter and pay on arrival.<br>
               <span style="font-size:0.65rem;">Order Reference: ${escapeHtml(orderData.payment_reference)}</span>
           </div>`
        : `<div class="receipt-footer">
               <i class="fa-regular fa-envelope"></i> A receipt has been sent to your email.<br>
               <span style="font-size:0.65rem;">Payment via Monnify | Transaction ID: ${escapeHtml(orderData.payment_reference)}</span>
           </div>`;

    dom.receiptBody.innerHTML = `
        <div class="receipt-row"><span class="label">Receipt #</span><span class="value" style="font-family:monospace;">${escapeHtml(orderData.payment_reference)}</span></div>
        <div class="receipt-row"><span class="label">Customer</span><span class="value">${escapeHtml(orderData.customer_name)}</span></div>
        <div class="receipt-row"><span class="label">Email</span><span class="value">${escapeHtml(orderData.customer_email)}</span></div>
        <div class="receipt-row"><span class="label">Phone</span><span class="value">${escapeHtml(orderData.customer_phone)}</span></div>
        <div class="receipt-row"><span class="label">Date</span><span class="value">${new Date().toLocaleString()}</span></div>
        <div class="receipt-row"><span class="label">Delivery</span><span class="value">${deliveryMethodLabel}</span></div>
        ${orderData.preferred_time ? `<div class="receipt-row"><span class="label">Preferred Time</span><span class="value">${escapeHtml(orderData.preferred_time)}</span></div>` : ''}
        <hr class="receipt-divider">
        <div class="receipt-items">${itemsHtml}</div>
        ${deliveryHtml}
        <hr class="receipt-divider">
        <div class="receipt-total">
            <div class="receipt-row" style="font-size:1.1rem;font-weight:800;padding-top:8px;border-top:2px solid var(--primary);margin-top:4px;">
                <span class="label">${totalLabel}</span>
                <span class="value" style="color:var(--primary);">${formatPrice(orderData.total)}</span>
            </div>
        </div>
        ${footerHtml}
        <div class="receipt-download-wrap">
            <button class="btn-primary" id="receipt-download-btn" type="button">
                <i class="fa-solid fa-file-pdf"></i> Download PDF Receipt
            </button>
        </div>
    `;
    dom.receiptOverlay.classList.add('open');
    document.body.style.overflow = 'hidden';

    const dlBtn = document.getElementById('receipt-download-btn');
    if (dlBtn) dlBtn.addEventListener('click', downloadReceiptPDF);
}

// ─── RECEIPT PDF DOWNLOAD ───
export async function downloadReceiptPDF() {
    if (typeof html2pdf === 'undefined') {
        showToast('PDF library not loaded. Please try again.', 'fa-solid fa-exclamation-circle');
        return;
    }
    const btn = document.getElementById('receipt-download-btn');
    if (!btn) return;
    const original = btn.innerHTML;
    btn.innerHTML = '<span class="loader-small"></span> Generating PDF...';
    btn.disabled = true;

    try {
        const clone = dom.receiptModal.cloneNode(true);
        const cClose = clone.querySelector('.receipt-close');
        if (cClose) cClose.remove();
        const cDl = clone.querySelector('#receipt-download-btn');
        if (cDl) cDl.parentElement.remove();
        const cHeader = clone.querySelector('.receipt-header');
        if (cHeader) cHeader.style.position = 'static';

        const ref = (state.lastOrderData && state.lastOrderData.payment_reference) || ('order-' + Date.now());
        await html2pdf().set({
            margin: 0,
            filename: `HotPortion-Receipt-${ref}.pdf`,
            image: { type: 'jpeg', quality: 0.98 },
            html2canvas: { scale: 2, useCORS: true, backgroundColor: '#ffffff' },
            jsPDF: { unit: 'mm', format: 'a4', orientation: 'portrait' }
        }).from(clone).save();

        showToast('Receipt downloaded!', 'fa-solid fa-circle-check');
    } catch (err) {
        console.error('PDF generation failed:', err);
        showToast('Failed to generate PDF. Please try again.', 'fa-solid fa-exclamation-circle');
    } finally {
        btn.innerHTML = original;
        btn.disabled = true;
        btn.disabled = false;
    }
}

export function closeReceipt() {
    dom.receiptOverlay.classList.remove('open');
    document.body.style.overflow = '';
    state.cart = {};
    updateCartUI();
    closeCart();
    dom.customerName.value = '';
    dom.customerEmail.value = '';
    dom.customerPhone.value = '';
    dom.deliveryAddress.value = '';
    dom.orderNotes.value = '';
    dom.preferredTime.value = '';
    state.isDeliveryCovered = false;
    state.deliveryFee = 0;
    state.lastBreakdown = null;
    state.checkoutIdempotencyKey = null;
    try { sessionStorage.removeItem(CHECKOUT_IDEMPOTENCY_KEY); } catch (e) { /* ignore */ }
    resetDeliveryStatus();
    updateDeliveryFeeUI(0);
    dom.deliveryFeeRow.style.display = 'none';
    dom.deliveryBreakdown.classList.remove('show');
    clearPersistedState();
    showToast('✅ Order placed successfully!', 'fa-solid fa-check-circle');
    refreshProducts();
}
