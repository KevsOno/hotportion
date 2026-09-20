import { state } from './state.js';
import { bindImageFallbacks } from './utils.js';
import { initData } from './menu.js';
import { setDeliveryMethod, setPaymentMethod } from './checkout.js';
import './ui.js'; // side-effect: attaches all event listeners

// Initial state: apply restored delivery method, then restore the payment
// method only if the current delivery method allows it.
setDeliveryMethod(state._restoredDeliveryMethod || 'pickup');
if ((state._restoredDeliveryMethod || 'pickup') !== 'delivery') {
    setPaymentMethod(state._restoredPaymentMethod || 'online');
}

// ─── BOOT ───
initData().then(() => {
    bindImageFallbacks();
    console.log('🚀 Hot Portion Grill ready!');
    console.log('📍 Delivery coverage with intelligent fee calculation');
    console.log('📊 Fee breakdown includes: base + discounts + surcharges');
    console.log('🔄 Delivery fee recalculates when cart changes');
    console.log('💾 Cart persists across page reloads');
    console.log('🧾 Receipt PDF download enabled');
    console.log('🔎 Order tracking enabled');
    console.log('💵 Offline payment ready (pickup / dine-in)');
});

console.log('📱 Bottom nav: Home, Menu, Chat, Cart (mobile only)');
console.log('🔐 Monnify payment integration ready');
console.log('🤖 Context-aware AI assistant active');
console.log('🎠 Hero slider dynamic – loaded from backend');
console.log('🖼️ All images served via Netlify Image CDN');
console.log('🔍 Address autocomplete via Amazon Location Service (proxied through backend)');
console.log('💰 Intelligent delivery fee with breakdown display');
