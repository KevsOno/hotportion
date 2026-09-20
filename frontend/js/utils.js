import { dom } from './dom.js';

// ─── HTML ESCAPING HELPER ───
// Converts HTML special characters to entities so that user-controlled
// strings are rendered as literal text instead of being parsed as markup.
// This is the primary defense against stored/reflected XSS at
// interpolation sites.
export function escapeHtml(v) {
    if (v === null || v === undefined) return '';
    return String(v)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

export function formatPrice(amount) {
    return '₦' + Number(amount).toLocaleString();
}

let toastTimer = null;

export function showToast(msg, icon = 'fa-solid fa-check-circle') {
    dom.toastMsg.textContent = msg;
    dom.toast.querySelector('i').className = icon;
    dom.toast.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => dom.toast.classList.remove('show'), 2500);
}

// ─── Netlify Image CDN wrapper ───
export function netlifyImageUrl(path, width = 400) {
    const base =
        'https://ajmauddonufaryjbiavh.supabase.co/storage/v1/object/public/bucket/fastfood/images/';
    const url = base + path.replace(/^\/fastfood\/images\//, '');
    return `/.netlify/images?url=${encodeURIComponent(url)}&w=${width}&format=webp`;
}

export function highlightText(text, query) {
    if (!query || !text) return text;
    const regex = new RegExp(`(${query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`, 'gi');
    return text.replace(regex, '<span class="highlight">$1</span>');
}

// ─── Image fallback binding ───
// Replaces inline onerror attributes so that script-src can drop
// 'unsafe-inline' without breaking image fallbacks.
export function bindImageFallbacks(root = document) {
    root.querySelectorAll('img[data-fallback-html]').forEach(img => {
        if (img.dataset.fallbackBound) return;
        img.dataset.fallbackBound = '1';
        img.addEventListener('error', function () {
            if (this.parentElement) {
                this.parentElement.innerHTML = this.dataset.fallbackHtml;
            }
        });
    });

    root.querySelectorAll('img[data-hide-on-error]').forEach(img => {
        if (img.dataset.hideBound) return;
        img.dataset.hideBound = '1';
        img.addEventListener('error', function () { this.style.display = 'none'; });
    });
}
