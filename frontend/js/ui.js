import { state } from './state.js';
import { dom } from './dom.js';
import { handleDeliveryAddressChange, handleGPSButton, searchAddresses, setDeliveryDebounceTimer } from './delivery.js';
import { updateCheckoutButton, checkout, setPaymentMethod, setDeliveryMethod } from './checkout.js';
import { addToCart, updateCartUI, persistState } from './cart.js';
import { toggleChat, sendChatMessage } from './chat.js';
import { openTrackModal, closeTrackModal, handleTrackSubmit } from './track.js';
import { performSearch } from './menu.js';
import { closeReceipt } from './receipt.js';
import { heroPrevBtn, heroNextBtn, prevHeroSlide, nextHeroSlide, stopHeroAutoplay, startHeroAutoplay } from './hero.js';

// ─── Set header height for sticky bar ───
export function setHeaderHeight() {
    const header = document.querySelector('.header');
    if (header) {
        const height = header.offsetHeight;
        document.documentElement.style.setProperty('--header-height', height + 'px');
    }
}
window.addEventListener('load', setHeaderHeight);
window.addEventListener('resize', setHeaderHeight);

// ─── Cart drawer ───
export function openCart() {
    dom.cartDrawer.classList.add('open');
    dom.cartOverlay.classList.add('open');
    document.body.style.overflow = 'hidden';
    updateCheckoutButton();
    if (state.deliveryMethod === 'delivery' && dom.deliveryAddress.value.trim().length >= 5) {
        state.lastCheckedAddress = '';
        setTimeout(handleDeliveryAddressChange, 200);
    }
}

export function closeCart() {
    dom.cartDrawer.classList.remove('open');
    dom.cartOverlay.classList.remove('open');
    document.body.style.overflow = '';
}

// ─── Bottom nav ───
export function setActiveNav(activeId) {
    document.querySelectorAll('.bottom-nav .nav-item').forEach(el => el.classList.remove('active'));
    const el = document.getElementById(activeId);
    if (el) el.classList.add('active');
}

// ─── Hero slider controls ───
if (heroPrevBtn) heroPrevBtn.addEventListener('click', () => { stopHeroAutoplay();
    prevHeroSlide();
    startHeroAutoplay(); });
if (heroNextBtn) heroNextBtn.addEventListener('click', () => { stopHeroAutoplay();
    nextHeroSlide();
    startHeroAutoplay(); });

const heroElement = document.querySelector('.hero');
if (heroElement) {
    heroElement.addEventListener('mouseenter', stopHeroAutoplay);
    heroElement.addEventListener('mouseleave', startHeroAutoplay);
    heroElement.addEventListener('touchstart', stopHeroAutoplay, { passive: true });
    heroElement.addEventListener('touchend', startHeroAutoplay, { passive: true });
}

// ─── EVENT LISTENERS ───
dom.cartToggle.addEventListener('click', openCart);
dom.cartClose.addEventListener('click', closeCart);
dom.cartOverlay.addEventListener('click', closeCart);

dom.orderNowBtn.addEventListener('click', function() {
    document.getElementById('menu-section').scrollIntoView({ behavior: 'smooth' });
    setTimeout(openCart, 500);
});
dom.heroOrderBtn.addEventListener('click', function() {
    document.getElementById('menu-section').scrollIntoView({ behavior: 'smooth' });
    setTimeout(openCart, 500);
});
dom.heroMenuBtn.addEventListener('click', function() {
    document.getElementById('menu-section').scrollIntoView({ behavior: 'smooth' });
});
dom.footerCart.addEventListener('click', function(e) { e.preventDefault();
    openCart(); });
dom.checkoutBtn.addEventListener('click', checkout);

document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        if (dom.cartDrawer.classList.contains('open')) closeCart();
        if (dom.chatModal.classList.contains('active')) dom.chatModal.classList.remove('active');
        if (dom.receiptOverlay.classList.contains('open')) closeReceipt();
        if (dom.addressDropdown.classList.contains('show')) dom.addressDropdown.classList.remove('show');
        if (dom.trackOverlay.classList.contains('open')) closeTrackModal();
    }
});

dom.deliveryOptions.forEach(btn => {
    btn.addEventListener('click', function() { setDeliveryMethod(this.dataset.method); });
});

dom.paymentOptions.forEach(btn => {
    btn.addEventListener('click', function() {
        setPaymentMethod(this.dataset.payment);
    });
});

// ─── DELIVERY ADDRESS INPUT WITH AUTOCOMPLETE ───
let addressAutocompleteTimeout = null;

dom.deliveryAddress.addEventListener('input', function() {
    const query = this.value.trim();
    persistState();

    clearTimeout(addressAutocompleteTimeout);
    if (query.length < 2) {
        dom.addressDropdown.classList.remove('show');
    } else {
        addressAutocompleteTimeout = setTimeout(() => {
            searchAddresses(query);
        }, 300);
    }

    setDeliveryDebounceTimer(() => {}, 0);
    if (query.length < 5) {
        import('./delivery.js').then(({ resetDeliveryStatus, updateDeliveryFeeUI }) => {
            resetDeliveryStatus();
            state.deliveryFee = 0;
            state.lastBreakdown = null;
            updateDeliveryFeeUI(0);
            dom.deliveryBreakdown.classList.remove('show');
            updateCartUI();
            updateCheckoutButton();
        }).catch(() => {});
        return;
    }
    setDeliveryDebounceTimer(handleDeliveryAddressChange, 500);
});

dom.deliveryAddress.addEventListener('focus', function() {
    if (this.value.trim().length >= 2) {
        dom.addressDropdown.classList.add('show');
    }
});

dom.deliveryAddress.addEventListener('blur', function() {
    setTimeout(() => {
        dom.addressDropdown.classList.remove('show');
    }, 300);
});

dom.deliveryAddress.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        dom.addressDropdown.classList.remove('show');
    }
    if (e.key === 'Enter') {
        const firstItem = dom.addressDropdown.querySelector('.address-autocomplete-item');
        if (firstItem) {
            firstItem.click();
        }
    }
});

// ─── FEE BREAKDOWN TOGGLE ───
dom.feeBreakdownToggle.addEventListener('click', function() {
    dom.deliveryBreakdown.classList.toggle('show');
    this.textContent = dom.deliveryBreakdown.classList.contains('show') ? 'Hide breakdown' : 'Show breakdown';
});

// ─── GPS BUTTON ───
dom.gpsBtn.addEventListener('click', handleGPSButton);

// ─── FORM INPUTS FOR CHECKOUT BUTTON STATE + PERSISTENCE ───
[dom.customerName, dom.customerEmail, dom.customerPhone].forEach(input => {
    input.addEventListener('input', function() {
        updateCheckoutButton();
        persistState();
    });
});
[dom.preferredTime, dom.orderNotes].forEach(input => {
    input.addEventListener('input', persistState);
});

// ─── Recalculate delivery fee when email changes (for loyalty) ───
dom.customerEmail.addEventListener('input', function() {
    if (state.deliveryMethod === 'delivery' && dom.deliveryAddress.value.trim().length >= 5) {
        state.lastCheckedAddress = '';
        setDeliveryDebounceTimer(handleDeliveryAddressChange, 500);
    }
});

// ─── Chat ───
dom.chatLauncher.addEventListener('click', toggleChat);
dom.chatBubble.addEventListener('click', function(e) { e.stopPropagation();
    toggleChat(); });
dom.closeChat.addEventListener('click', function() { dom.chatModal.classList.remove('active'); });
dom.chatSend.addEventListener('click', sendChatMessage);
dom.chatInput.addEventListener('keypress', function(e) {
    if (e.key === 'Enter') { e.preventDefault();
        sendChatMessage(); }
});
document.querySelectorAll('.chat-quick-prompt').forEach(btn => {
    btn.addEventListener('click', function() {
        const prompt = this.dataset.chatPrompt;
        if (!dom.chatModal.classList.contains('active')) toggleChat();
        dom.chatInput.value = prompt;
        sendChatMessage();
    });
});

// ─── Search ───
dom.searchInput.addEventListener('input', function() {
    const val = this.value.trim();
    if (dom.searchClear) dom.searchClear.style.display = val ? 'block' : 'none';
    clearTimeout(window.searchDebounce);
    window.searchDebounce = setTimeout(performSearch, 300);
});
dom.searchClear.addEventListener('click', function() {
    dom.searchInput.value = '';
    state.searchQuery = '';
    dom.searchClear.style.display = 'none';
    performSearch();
    dom.searchInput.focus();
});
dom.searchBtn.addEventListener('click', performSearch);
dom.searchInput.addEventListener('keypress', function(e) { if (e.key === 'Enter') { e.preventDefault();
        performSearch(); } });

// ─── Bottom nav ───
dom.navHome.addEventListener('click', function() {
    setActiveNav('nav-home');
    window.scrollTo({ top: 0, behavior: 'smooth' });
});
dom.navMenu.addEventListener('click', function() {
    setActiveNav('nav-menu');
    document.getElementById('menu-section').scrollIntoView({ behavior: 'smooth' });
});
dom.navChat.addEventListener('click', function() {
    setActiveNav('nav-chat');
    if (!dom.chatModal.classList.contains('active')) toggleChat();
    else dom.chatModal.classList.remove('active');
});
dom.navCartBottom.addEventListener('click', function() {
    setActiveNav('nav-cart-bottom');
    openCart();
});

// ─── Track order modal ───
if (dom.trackCloseBtn) dom.trackCloseBtn.addEventListener('click', closeTrackModal);
if (dom.trackOverlay) dom.trackOverlay.addEventListener('click', function(e) {
    if (e.target === this) closeTrackModal();
});
if (dom.trackSubmit) dom.trackSubmit.addEventListener('click', handleTrackSubmit);
if (dom.trackRefInput) dom.trackRefInput.addEventListener('keypress', function(e) {
    if (e.key === 'Enter') { e.preventDefault(); handleTrackSubmit(); }
});
if (dom.trackEmailInput) dom.trackEmailInput.addEventListener('keypress', function(e) {
    if (e.key === 'Enter') { e.preventDefault(); handleTrackSubmit(); }
});
if (dom.footerTrack) dom.footerTrack.addEventListener('click', function(e) {
    e.preventDefault();
    openTrackModal();
});

// ─── Receipt close ───
dom.receiptClose.addEventListener('click', closeReceipt);
dom.receiptOverlay.addEventListener('click', function(e) { if (e.target === this) closeReceipt(); });

// ─── Resize: re-render featured ───
let resizeTimeout;
window.addEventListener('resize', function() {
    clearTimeout(resizeTimeout);
    resizeTimeout = setTimeout(() => {
        if (window.featuredInterval) clearInterval(window.featuredInterval);
        import('./menu.js').then(({ renderFeatured }) => renderFeatured()).catch(() => {});
    }, 300);
});
