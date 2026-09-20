import { state } from './state.js';
import { dom } from './dom.js';
import { formatPrice, bindImageFallbacks } from './utils.js';
import { loadProductsFromAPI, loadBannersFromAPI } from './api.js';
import { addToCart, updateCartUI } from './cart.js';
import { handlePaymentRedirect } from './checkout.js';
import { initChat } from './chat.js';
import { initHeroDots, startHeroAutoplay, goToHeroSlide, stopHeroAutoplay } from './hero.js';
import { openCart } from './ui.js';

// ─── PRODUCT REFRESH ───
export async function refreshProducts() {
    await loadProductsFromAPI();
    renderCategoriesPills();
    renderMenu();
    renderFeatured();
    console.log('🔄 Products refreshed after stock adjustment');
}

// ─── INIT DATA ───
export async function initData() {
    if (dom.shimmerWrapper) dom.shimmerWrapper.style.display = 'grid';

    await loadProductsFromAPI();
    state.isDataLoaded = true;

    if (window.__injectProductSchemas && state.products.length) {
        window.__injectProductSchemas(state.products);
        if (typeof window.__updateFeaturedItems === 'function') {
            window.__updateFeaturedItems(state.products);
        }
    }

    await loadBannersFromAPI();

    if (dom.shimmerWrapper) dom.shimmerWrapper.style.display = 'none';

    renderCategoriesPills();
    renderMenu();
    renderFeatured();
    updateCartUI();
    initChat();

    initHeroDots();
    startHeroAutoplay();

    handlePaymentRedirect();

    console.log(`🍔 Hot Portion Grill — ${state.products.length} meals, ${state.categories.length} categories`);
}

// ─── FILTERED PRODUCTS ───
export function getFilteredProducts() {
    let filtered = state.products;
    if (state.searchQuery.trim()) {
        const q = state.searchQuery.trim().toLowerCase();
        filtered = filtered.filter(p =>
            p.name.toLowerCase().includes(q) ||
            p.description.toLowerCase().includes(q) ||
            p.tag.toLowerCase().includes(q)
        );
    }
    if (state.selectedCategory) {
        filtered = filtered.filter(p => p.tag === state.selectedCategory);
    }
    return filtered;
}

// ─── RENDER CATEGORY PILLS ───
export function renderCategoriesPills() {
    if (!dom.categoriesPills) return;
    let html = '';
    const allActive = state.selectedCategory === '' ? 'active' : '';
    html +=
        `<span class="cat-pill ${allActive}" data-category="">All <span class="pill-count">(${state.products.length})</span></span>`;
    state.categories.forEach(cat => {
        const count = state.products.filter(p => p.tag === cat).length;
        const isActive = state.selectedCategory === cat ? 'active' : '';
        html +=
            `<span class="cat-pill ${isActive}" data-category="${cat}">${cat} <span class="pill-count">(${count})</span></span>`;
    });
    dom.categoriesPills.innerHTML = html;

    dom.categoriesPills.querySelectorAll('.cat-pill').forEach(pill => {
        pill.addEventListener('click', function() {
            const cat = this.dataset.category;
            filterByCategory(cat);
        });
    });
}

// ─── FILTER BY CATEGORY ───
export function filterByCategory(cat) {
    if (state.selectedCategory === cat) {
        state.selectedCategory = '';
    } else {
        state.selectedCategory = cat;
    }
    renderCategoriesPills();
    dom.searchInput.value = '';
    state.searchQuery = '';
    dom.searchClear.style.display = 'none';
    performSearch();
    document.getElementById('menu-section').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// ─── RENDER MENU ───
export function renderMenu() {
    if (!state.isDataLoaded) return;
    const filtered = getFilteredProducts();
    dom.menuGrid.innerHTML = '';
    if (filtered.length === 0) {
        dom.menuGrid.innerHTML =
            `<div class="col-span-full text-center py-12 text-gray-500"><i class="fa-regular fa-face-frown text-2xl"></i><p class="mt-2">No meals match your search.</p></div>`;
        return;
    }
    filtered.forEach((p, idx) => {
        const card = document.createElement('div');
        card.className = 'menu-card';
        card.style.animationDelay = (idx * 0.04) + 's';
        const tagColor = p.tagColor === 'orange' ? 'orange' : '';
        const imageHtml = p.image ?
            `<img src="${p.image}" alt="${p.name}" loading="lazy" width="300" height="200" data-hide-on-error>` :
            `<span class="food-emoji">${p.emoji || '🍽️'}</span>`;
        card.innerHTML = `
            <div class="menu-card-image">
                <span class="emoji-bg">${p.emoji || '🍽️'}</span>
                ${imageHtml}
                <span class="tag ${tagColor}">${p.tag}</span>
            </div>
            <div class="menu-card-body">
                <h3>${p.name}</h3>
                <p class="desc">${p.description}</p>
                <div class="price">${formatPrice(p.price)}</div>
                <div class="actions">
                    <div class="qty-control" data-product-id="${p.id}">
                        <button class="minus" aria-label="Decrease quantity">−</button>
                        <span class="qty-num">1</span>
                        <button class="plus" aria-label="Increase quantity">+</button>
                    </div>
                    <button class="btn-primary add-to-cart" data-id="${p.id}">
                        <i class="fa-solid fa-plus"></i> Add
                    </button>
                </div>
            </div>
        `;
        dom.menuGrid.appendChild(card);
    });

    document.querySelectorAll('.qty-control').forEach(ctrl => {
        const minus = ctrl.querySelector('.minus');
        const plus = ctrl.querySelector('.plus');
        const num = ctrl.querySelector('.qty-num');
        minus.addEventListener('click', function(e) {
            e.stopPropagation();
            let val = parseInt(num.textContent) || 1;
            if (val > 1) val--;
            num.textContent = val;
        });
        plus.addEventListener('click', function(e) {
            e.stopPropagation();
            let val = parseInt(num.textContent) || 1;
            val++;
            num.textContent = val;
        });
    });

    document.querySelectorAll('.add-to-cart').forEach(btn => {
        btn.addEventListener('click', function(e) {
            e.stopPropagation();
            const id = this.dataset.id;
            const card = this.closest('.menu-card');
            const qtyCtrl = card.querySelector('.qty-control');
            const qtyNum = qtyCtrl.querySelector('.qty-num');
            const qty = parseInt(qtyNum.textContent) || 1;
            addToCart(id, qty);
        });
    });

    bindImageFallbacks();
}

// ─── RENDER FEATURED CAROUSEL ───
export function renderFeatured() {
    if (!dom.featuredGrid || !state.isDataLoaded) return;
    const featuredItems = state.products.slice(0, 8);
    const isMobile = window.innerWidth <= 640;
    const itemsPerSlide = isMobile ? 1 : 2;

    const slides = [];
    for (let i = 0; i < featuredItems.length; i += itemsPerSlide) {
        slides.push(featuredItems.slice(i, i + itemsPerSlide));
    }

    dom.featuredGrid.innerHTML = '';
    const carousel = document.createElement('div');
    carousel.className = 'featured-carousel';

    slides.forEach((chunk, idx) => {
        const slide = document.createElement('div');
        slide.className = `featured-slide${idx === 0 ? ' active' : ''}`;
        slide.dataset.index = idx;
        chunk.forEach(p => {
            const card = document.createElement('div');
            card.className = 'pf-product-card menu-card';
            card.style.animation = 'none';
            const tagColor = p.tagColor === 'orange' ? 'orange' : '';
            const imageHtml = p.image ?
                `<img src="${p.image}" alt="${p.name}" loading="lazy" width="300" height="200" data-hide-on-error>` :
                `<span class="food-emoji">${p.emoji || '🍽️'}</span>`;
            card.innerHTML = `
                <div class="menu-card-image">
                    <span class="emoji-bg">${p.emoji || '🍽️'}</span>
                    ${imageHtml}
                    <span class="tag ${tagColor}">${p.tag}</span>
                </div>
                <div class="menu-card-body">
                    <h3>${p.name}</h3>
                    <p class="desc">${p.description}</p>
                    <div class="price">${formatPrice(p.price)}</div>
                    <div class="actions">
                        <div class="qty-control" data-product-id="${p.id}">
                            <button class="minus" aria-label="Decrease quantity">−</button>
                            <span class="qty-num">1</span>
                            <button class="plus" aria-label="Increase quantity">+</button>
                        </div>
                        <button class="btn-primary add-to-cart" data-id="${p.id}">
                            <i class="fa-solid fa-plus"></i> Add
                        </button>
                    </div>
                </div>
            `;
            slide.appendChild(card);
        });
        carousel.appendChild(slide);
    });

    dom.featuredGrid.appendChild(carousel);

    dom.featuredGrid.querySelectorAll('.qty-control').forEach(ctrl => {
        const minus = ctrl.querySelector('.minus');
        const plus = ctrl.querySelector('.plus');
        const num = ctrl.querySelector('.qty-num');
        minus.addEventListener('click', function(e) {
            e.stopPropagation();
            let val = parseInt(num.textContent) || 1;
            if (val > 1) val--;
            num.textContent = val;
        });
        plus.addEventListener('click', function(e) {
            e.stopPropagation();
            let val = parseInt(num.textContent) || 1;
            val++;
            num.textContent = val;
        });
    });
    dom.featuredGrid.querySelectorAll('.add-to-cart').forEach(btn => {
        btn.addEventListener('click', function(e) {
            e.stopPropagation();
            const id = this.dataset.id;
            const card = this.closest('.menu-card');
            const qtyCtrl = card.querySelector('.qty-control');
            const qtyNum = qtyCtrl.querySelector('.qty-num');
            const qty = parseInt(qtyNum.textContent) || 1;
            addToCart(id, qty);
        });
    });

    let current = 0;
    const totalSlides = slides.length;
    if (totalSlides > 1) {
        if (window.featuredInterval) clearInterval(window.featuredInterval);
        window.featuredInterval = setInterval(() => {
            const slidesElements = dom.featuredGrid.querySelectorAll('.featured-slide');
            slidesElements.forEach((s, i) => s.classList.toggle('active', i === current));
            current = (current + 1) % totalSlides;
        }, 4000);
    }

    bindImageFallbacks();
}

// ─── SEARCH ───
export function performSearch() {
    state.searchQuery = dom.searchInput.value.trim();
    if (dom.searchClear) {
        dom.searchClear.style.display = state.searchQuery ? 'block' : 'none';
    }
    renderMenu();
    if (window.innerWidth <= 768) {
        document.getElementById('menu-section').scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
}
