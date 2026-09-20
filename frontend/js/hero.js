import { state } from './state.js';
import { openCart } from './ui.js';

let heroCurrentIndex = 0;
const heroDotsContainer = document.getElementById('hero-dots');
const heroPrevBtn = document.getElementById('hero-prev');
const heroNextBtn = document.getElementById('hero-next');
let heroInterval = null;
let isHeroTransitioning = false;

// ─── RENDER HERO SLIDES ───
export function renderHeroSlides() {
    const slider = document.getElementById('hero-slider');
    if (!slider) return;

    if (!state.banners || state.banners.length === 0) {
        console.log('No active banners to display');
        return;
    }

    const slides = slider.querySelectorAll('.hero-slide');
    slides.forEach(s => s.remove());

    let html = '';
    state.banners.forEach((banner, index) => {
        const activeClass = index === 0 ? 'active' : '';
        const discountDisplay = banner.discount_value ?
            `<div class="hero-offer">
                <span class="percent">${banner.discount_type === 'percentage' ? banner.discount_value + '%' : '₦' + banner.discount_value}</span>
                <span class="label">
                    ${banner.discount_type === 'percentage' ? 'OFF' : 'OFF'} 
                    <small>${banner.discount_type === 'percentage' ? 'Limited time!' : 'Save now!'}</small>
                </span>
            </div>` : '';

        const ctaHtml = banner.cta_text ?
            `<div class="hero-cta">
                <button class="btn-primary" style="background:${banner.badge_color || '#E53935'};">
                    <i class="fa-solid fa-bag-shopping"></i> ${banner.cta_text}
                </button>
            </div>` : '';

        html += `
            <div class="hero-slide ${activeClass}" data-index="${index}">
                ${banner.badge_text ? `<span class="hero-badge" style="background:${banner.badge_color || '#FFC107'};">${banner.badge_text}</span>` : ''}
                <h1 style="color:${banner.text_color || '#1e1e1e'};">
                    ${banner.title}
                    ${banner.subtitle ? `<br><span style="color:${banner.badge_color || '#E53935'};">${banner.subtitle}</span>` : ''}
                </h1>
                ${banner.description ? `<p style="color:${banner.text_color || '#4a4a4a'};}">${banner.description}</p>` : ''}
                ${discountDisplay}
                ${ctaHtml}
            </div>
        `;
    });

    const prevArrow = slider.querySelector('.hero-arrow.prev');
    if (prevArrow) {
        prevArrow.insertAdjacentHTML('beforebegin', html);
    } else {
        slider.innerHTML = html + slider.innerHTML;
    }

    if (heroDotsContainer) {
        heroDotsContainer.innerHTML = '';
        state.banners.forEach((_, i) => {
            const dot = document.createElement('button');
            dot.className = `hero-dot${i === 0 ? ' active' : ''}`;
            dot.setAttribute('role', 'tab');
            dot.setAttribute('aria-label', `Slide ${i + 1}`);
            dot.addEventListener('click', () => goToHeroSlide(i));
            heroDotsContainer.appendChild(dot);
        });
    }

    heroCurrentIndex = 0;
    isHeroTransitioning = false;

    document.querySelectorAll('.hero-slide .hero-cta .btn-primary').forEach(btn => {
        btn.addEventListener('click', function(e) {
            e.stopPropagation();
            document.getElementById('menu-section').scrollIntoView({ behavior: 'smooth' });
            setTimeout(openCart, 500);
        });
    });

    stopHeroAutoplay();
    startHeroAutoplay();
}

export function initHeroDots() {
    if (!heroDotsContainer) return;
    const slides = document.querySelectorAll('.hero-slide');
    heroDotsContainer.innerHTML = '';
    slides.forEach((slide, i) => {
        const dot = document.createElement('button');
        dot.className = `hero-dot${i === 0 ? ' active' : ''}`;
        dot.setAttribute('role', 'tab');
        dot.setAttribute('aria-label', `Slide ${i + 1}`);
        dot.addEventListener('click', () => goToHeroSlide(i));
        heroDotsContainer.appendChild(dot);
    });
}

export function goToHeroSlide(index) {
    const slides = document.querySelectorAll('.hero-slide');
    if (slides.length === 0) return;
    if (isHeroTransitioning || index === heroCurrentIndex || index >= slides.length) return;
    isHeroTransitioning = true;

    slides[heroCurrentIndex].classList.remove('active');
    const dots = heroDotsContainer.querySelectorAll('.hero-dot');
    if (dots[heroCurrentIndex]) dots[heroCurrentIndex].classList.remove('active');

    heroCurrentIndex = index;

    slides[heroCurrentIndex].classList.add('active');
    if (dots[heroCurrentIndex]) dots[heroCurrentIndex].classList.add('active');

    setTimeout(() => {
        isHeroTransitioning = false;
    }, 800);
}

export function nextHeroSlide() {
    const slides = document.querySelectorAll('.hero-slide');
    if (slides.length === 0) return;
    const next = (heroCurrentIndex + 1) % slides.length;
    goToHeroSlide(next);
}

export function prevHeroSlide() {
    const slides = document.querySelectorAll('.hero-slide');
    if (slides.length === 0) return;
    const prev = (heroCurrentIndex - 1 + slides.length) % slides.length;
    goToHeroSlide(prev);
}

export function startHeroAutoplay() {
    if (heroInterval) clearInterval(heroInterval);
    heroInterval = setInterval(nextHeroSlide, 5000);
}

export function stopHeroAutoplay() {
    if (heroInterval) {
        clearInterval(heroInterval);
        heroInterval = null;
    }
}

export { heroPrevBtn, heroNextBtn };
