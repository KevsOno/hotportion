/*
 * Hot Portion Grill — Admin Panel
 * Extracted from admin.html to comply with the site's Content Security
 * Policy (CSP), which forbids inline <script> execution.
 *
 * Loaded with `defer` from admin.html so the DOM is fully parsed before
 * this runs. The external libraries it depends on — Supabase JS, Leaflet,
 * and Leaflet Draw — are loaded synchronously in admin.html BEFORE this
 * file, so they are guaranteed to be present on `window` when we start.
 *
 * Everything is wrapped in an IIFE so nothing leaks into global scope.
 */
(function() {
    'use strict';

    // ═══════════════════════════════════════════════════════
    // HTML ESCAPING HELPER
    // ═══════════════════════════════════════════════════════
    // Converts HTML special characters to entities so that
    // user-controlled strings are rendered as literal text
    // instead of being parsed as markup. This is the primary
    // defense against stored/reflected XSS at interpolation sites.
    function escapeHtml(v) {
        if (v === null || v === undefined) return '';
        return String(v)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    // ═══════════════════════════════════════════════════════
    // AUTH CONFIG & SUPABASE CLIENT
    // ═══════════════════════════════════════════════════════
    const AUTH_CONFIG = {
        SUPABASE_URL: 'https://ajmauddonufaryjbiavh.supabase.co',
        SUPABASE_ANON_KEY: 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImFqbWF1ZGRvbnVmYXJ5amJpYXZoIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODg2MTM4NDcsImV4cCI6MjEwNDE4OTg0N30.uqBgJan_20vpOEaWqJD7Lf95WXV5fx-xDDPqTekxTdQ',
        DEMO_EMAIL: 'demo@123.com',
        DEMO_PASSWORD: 'dem123',
    };

    if (!window.supabase || !window.supabase.createClient) {
        document.body.innerHTML =
            '<div style="padding:40px;font-family:sans-serif;text-align:center;">' +
            '<h2 style="color:#E53935;">Supabase library failed to load</h2>' +
            '<p>Please refresh the page or check your internet connection.</p>' +
            '</div>';
        return;
    }

    const supabaseClient = window.supabase.createClient(
        AUTH_CONFIG.SUPABASE_URL,
        AUTH_CONFIG.SUPABASE_ANON_KEY,
        {
            auth: {
                persistSession: true,
                autoRefreshToken: true,
                detectSessionInUrl: false,
                storageKey: 'hotportion-admin-auth',
            }
        }
    );

    // ─── Explicit URL-hash session bootstrap ───
    // Still needed for the "Forgot password" recovery flow, which
    // redirects back with #access_token=...&type=recovery.
    (async function bootstrapSessionFromUrl() {
        try {
            const rawHash = (window.location.hash || '').replace(/^#/, '');
            if (!rawHash) return;
            const params = new URLSearchParams(rawHash);
            const access_token = params.get('access_token');
            const refresh_token = params.get('refresh_token');
            if (access_token && refresh_token) {
                const { error } = await supabaseClient.auth.setSession({
                    access_token,
                    refresh_token,
                });
                if (error) {
                    console.warn('setSession from URL failed:', error.message);
                } else {
                    console.log('✅ Session bootstrapped from URL hash');
                }
            }
        } catch (e) {
            console.warn('URL hash session bootstrap failed:', e);
        }
    })();

    // ─── STATE ───
    let currentStaff = null;
    let isSigningOut = false;

    // ═══════════════════════════════════════════════════════
    // API WRAPPER
    // ═══════════════════════════════════════════════════════
    async function getValidToken() {
        try {
            const { data: { session } } = await supabaseClient.auth.getSession();
            if (!session) return null;
            if (session.expires_at && (session.expires_at * 1000 - Date.now() < 60000)) {
                const { data: { session: fresh }, error } = await supabaseClient.auth.refreshSession();
                if (error || !fresh) return null;
                return fresh.access_token;
            }
            return session.access_token;
        } catch (e) {
            console.error('Token fetch error:', e);
            return null;
        }
    }

    async function apiFetch(url, options = {}) {
        const token = await getValidToken();
        const headers = { ...(options.headers || {}) };
        if (!(options.body instanceof FormData) && !headers['Content-Type']) {
            headers['Content-Type'] = 'application/json';
        }
        if (token) headers['Authorization'] = `Bearer ${token}`;
        const response = await fetch(url, { ...options, headers });
        if (response.status === 401) {
            console.warn('401 from', url, '— session invalid, signing out');
            await handleUnauthorized();
        }
        return response;
    }

    async function handleUnauthorized() {
        if (isSigningOut) return;
        isSigningOut = true;
        try { await supabaseClient.auth.signOut(); } catch (e) {}
        try { stopPolling(); } catch (e) {}
        showLoginScreen('Your session expired. Please sign in again.');
        isSigningOut = false;
    }

    // ═══════════════════════════════════════════════════════
    // "REMEMBER ME" HANDLING
    // ═══════════════════════════════════════════════════════
    const NO_PERSIST_KEY = 'hp_no_persist';

    function setNoPersist(value) {
        try {
            if (value) sessionStorage.setItem(NO_PERSIST_KEY, '1');
            else sessionStorage.removeItem(NO_PERSIST_KEY);
        } catch (e) {}
    }

    function shouldNotPersist() {
        try { return sessionStorage.getItem(NO_PERSIST_KEY) === '1'; }
        catch (e) { return false; }
    }

    window.addEventListener('pagehide', () => {
        if (!shouldNotPersist()) return;
        try {
            const keys = Object.keys(localStorage).filter(k =>
                k.startsWith('hotportion-admin-auth') || k.startsWith('sb-')
            );
            keys.forEach(k => localStorage.removeItem(k));
        } catch (e) {}
    });

    // ═══════════════════════════════════════════════════════
    // AUTH SCREEN CONTROL
    // ═══════════════════════════════════════════════════════
    const authGate = document.getElementById('auth-gate');
    const loginCard = document.getElementById('auth-login-card');
    const forgotCard = document.getElementById('auth-forgot-card');
    const adminApp = document.getElementById('admin-app');
    const setpwCard = document.getElementById('auth-setpw-card');

    function showLoginScreen(message) {
        currentStaff = null;
        authGate.classList.remove('hidden');
        adminApp.style.display = 'none';
        loginCard.style.display = '';
        forgotCard.style.display = 'none';
        setpwCard.style.display = 'none';

        const loginSuccess = document.getElementById('login-success');
        const loginError = document.getElementById('login-error');
        if (message) {
            loginSuccess.textContent = message;
            loginSuccess.classList.add('show');
        } else {
            loginSuccess.classList.remove('show');
        }
        loginError.classList.remove('show');

        resetForgotForms();
    }

    function hideAuthGate() {
        authGate.classList.add('hidden');
        adminApp.style.display = '';
    }

    function showForgotCard() {
        loginCard.style.display = 'none';
        forgotCard.style.display = '';
        setpwCard.style.display = 'none';
        resetForgotForms();
    }

    // Kept for the recovery flow. Invite flow is no longer used.
    function showSetPasswordCard(email, mode) {
        currentStaff = null;
        authGate.classList.remove('hidden');
        adminApp.style.display = 'none';
        loginCard.style.display = 'none';
        forgotCard.style.display = 'none';
        setpwCard.style.display = '';

        document.getElementById('setpw-email').textContent = email || 'your account';

        document.getElementById('setpw-error').classList.remove('show');
        document.getElementById('setpw-success').classList.remove('show');
        document.getElementById('setpw-password').value = '';
        document.getElementById('setpw-confirm').value = '';

        const heading = setpwCard.querySelector('h1');
        const subtitle = setpwCard.querySelector('.auth-subtitle');
        if (mode === 'recovery') {
            heading.innerHTML = 'Reset Your <span>Password</span>';
            subtitle.textContent = 'Choose a new password for your account';
        } else {
            heading.innerHTML = 'Welcome to <span>HotPortion</span>';
            subtitle.textContent = 'Set a password to finish setting up your account';
        }

        setTimeout(() => {
            const pwd = document.getElementById('setpw-password');
            if (pwd) pwd.focus();
        }, 100);
    }

    function resetForgotForms() {
        document.getElementById('forgot-email-form').style.display = '';
        document.getElementById('forgot-otp-form').style.display = 'none';
        document.getElementById('forgot-newpass-form').style.display = 'none';
        document.getElementById('forgot-error').classList.remove('show');
        document.getElementById('forgot-success').classList.remove('show');
        document.getElementById('forgot-email').value = '';
        document.getElementById('forgot-email').disabled = false;
        document.querySelectorAll('#otp-inputs input').forEach(i => i.value = '');
        document.getElementById('new-password').value = '';
        document.getElementById('confirm-password').value = '';
    }

    function showAuthError(id, msg) {
        const el = document.getElementById(id);
        el.textContent = msg;
        el.classList.add('show');
    }

    function showAuthSuccess(id, msg) {
        const el = document.getElementById(id);
        el.textContent = msg;
        el.classList.add('show');
    }

    // ═══════════════════════════════════════════════════════
    // LOGIN
    // ═══════════════════════════════════════════════════════
    const loginForm = document.getElementById('login-form');
    const loginError = document.getElementById('login-error');
    const loginSuccess = document.getElementById('login-success');
    const loginSubmit = document.getElementById('login-submit');
    const rememberMe = document.getElementById('remember-me');

    async function performLogin(email, password, remember) {
        loginError.classList.remove('show');
        loginSuccess.classList.remove('show');
        loginSubmit.disabled = true;
        const originalHtml = loginSubmit.innerHTML;
        loginSubmit.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Signing in...';

        try {
            const { data, error } = await supabaseClient.auth.signInWithPassword({
                email: email.trim(),
                password: password,
            });

            if (error) throw error;
            if (!data.session) throw new Error('No session returned');

            setNoPersist(!remember);

            const meRes = await apiFetch('/api/auth/me');
            if (!meRes.ok) {
                let detail = 'Could not verify your account.';
                try { const body = await meRes.json(); detail = body.detail || detail; } catch (e) {}
                await supabaseClient.auth.signOut();
                throw new Error(detail);
            }
            const me = await meRes.json();
            onAuthenticated(me);
        } catch (err) {
            console.error('Login error:', err);
            let msg = err.message || 'Login failed. Please try again.';
            if (msg.toLowerCase().includes('invalid login')) {
                msg = 'Invalid email or password.';
            } else if (msg.toLowerCase().includes('email not confirmed')) {
                msg = 'Please confirm your email first.';
            }
            showAuthError('login-error', msg);
        } finally {
            loginSubmit.disabled = false;
            loginSubmit.innerHTML = originalHtml;
        }
    }

    loginForm.addEventListener('submit', (e) => {
        e.preventDefault();
        const email = document.getElementById('login-email').value;
        const password = document.getElementById('login-password').value;
        const remember = rememberMe.checked;
        performLogin(email, password, remember);
    });

    document.getElementById('demo-login-btn').addEventListener('click', () => {
        document.getElementById('login-email').value = AUTH_CONFIG.DEMO_EMAIL;
        document.getElementById('login-password').value = AUTH_CONFIG.DEMO_PASSWORD;
        rememberMe.checked = false;
        performLogin(AUTH_CONFIG.DEMO_EMAIL, AUTH_CONFIG.DEMO_PASSWORD, false);
    });

    function wireEyeToggle(btnId, inputId, iconId) {
        const btn = document.getElementById(btnId);
        const input = document.getElementById(inputId);
        const icon = document.getElementById(iconId);
        if (!btn || !input || !icon) return;
        btn.addEventListener('click', () => {
            const isPassword = input.type === 'password';
            input.type = isPassword ? 'text' : 'password';
            icon.className = isPassword ? 'fa-regular fa-eye-slash' : 'fa-regular fa-eye';
            btn.setAttribute('aria-label', isPassword ? 'Hide password' : 'Show password');
        });
    }
    wireEyeToggle('toggle-login-password', 'login-password', 'eye-login-icon');
    wireEyeToggle('toggle-new-password', 'new-password', 'eye-new-icon');
    wireEyeToggle('toggle-confirm-password', 'confirm-password', 'eye-confirm-icon');
    wireEyeToggle('toggle-setpw-password', 'setpw-password', 'eye-setpw-icon');
    wireEyeToggle('toggle-setpw-confirm', 'setpw-confirm', 'eye-setpw-confirm-icon');

    // ═══════════════════════════════════════════════════════
    // FORGOT PASSWORD (OTP flow) — unchanged
    // ═══════════════════════════════════════════════════════
    let pendingResetEmail = '';

    document.getElementById('forgot-password-link').addEventListener('click', (e) => {
        e.preventDefault();
        showForgotCard();
    });

    document.getElementById('forgot-back').addEventListener('click', () => {
        forgotCard.style.display = 'none';
        loginCard.style.display = '';
    });

    document.getElementById('forgot-email-form').addEventListener('submit', async (e) => {
        e.preventDefault();
        const email = document.getElementById('forgot-email').value.trim();
        const btn = document.getElementById('forgot-send-btn');
        const errorEl = document.getElementById('forgot-error');
        errorEl.classList.remove('show');

        if (!email) {
            showAuthError('forgot-error', 'Please enter your email.');
            return;
        }

        btn.disabled = true;
        const orig = btn.innerHTML;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Sending...';

        try {
            const { error } = await supabaseClient.auth.signInWithOtp({
                email: email,
                options: { shouldCreateUser: false },
            });
            if (error) throw error;

            pendingResetEmail = email;
            document.getElementById('forgot-email-display').textContent = email;
            document.getElementById('forgot-email-form').style.display = 'none';
            document.getElementById('forgot-otp-form').style.display = '';
            document.getElementById('forgot-success').classList.remove('show');
            setTimeout(() => {
                const first = document.querySelector('#otp-inputs input[data-otp-index="0"]');
                if (first) first.focus();
            }, 100);
        } catch (err) {
            console.error('OTP send error:', err);
            let msg = err.message || 'Could not send OTP.';
            if (msg.toLowerCase().includes('user not found') || msg.toLowerCase().includes('signups')) {
                msg = 'No account found with that email.';
            }
            showAuthError('forgot-error', msg);
        } finally {
            btn.disabled = false;
            btn.innerHTML = orig;
        }
    });

    const otpInputs = Array.from(document.querySelectorAll('#otp-inputs input'));
    otpInputs.forEach((input, idx) => {
        input.addEventListener('input', (e) => {
            const v = e.target.value.replace(/\D/g, '');
            e.target.value = v.slice(0, 1);
            if (v && idx < otpInputs.length - 1) otpInputs[idx + 1].focus();
        });
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Backspace' && !e.target.value && idx > 0) {
                otpInputs[idx - 1].focus();
            }
        });
        input.addEventListener('paste', (e) => {
            e.preventDefault();
            const pasted = (e.clipboardData || window.clipboardData).getData('text').replace(/\D/g, '');
            if (!pasted) return;
            for (let i = 0; i < otpInputs.length && i < pasted.length; i++) {
                otpInputs[i].value = pasted[i];
            }
            const nextEmpty = otpInputs.findIndex(inp => !inp.value);
            const focusIdx = nextEmpty === -1 ? otpInputs.length - 1 : nextEmpty;
            otpInputs[focusIdx].focus();
        });
    });

    document.getElementById('forgot-otp-form').addEventListener('submit', async (e) => {
        e.preventDefault();
        const token = otpInputs.map(i => i.value).join('');
        const btn = document.getElementById('forgot-verify-btn');
        const errorEl = document.getElementById('forgot-error');
        errorEl.classList.remove('show');

        if (token.length !== 8) {
            showAuthError('forgot-error', 'Please enter all 8 digits.');
            return;
        }

        btn.disabled = true;
        const orig = btn.innerHTML;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Verifying...';

        try {
            const { data, error } = await supabaseClient.auth.verifyOtp({
                email: pendingResetEmail,
                token: token,
                type: 'email',
            });
            if (error) throw error;
            if (!data.session) throw new Error('Verification failed — no session returned.');

            document.getElementById('forgot-otp-form').style.display = 'none';
            document.getElementById('forgot-newpass-form').style.display = '';
            setTimeout(() => document.getElementById('new-password').focus(), 100);
        } catch (err) {
            console.error('OTP verify error:', err);
            let msg = err.message || 'Invalid or expired code.';
            if (msg.toLowerCase().includes('expired')) msg = 'Code expired. Please resend.';
            if (msg.toLowerCase().includes('invalid')) msg = 'Invalid code. Please check and try again.';
            showAuthError('forgot-error', msg);
        } finally {
            btn.disabled = false;
            btn.innerHTML = orig;
        }
    });

    document.getElementById('forgot-resend-btn').addEventListener('click', async () => {
        const btn = document.getElementById('forgot-resend-btn');
        const errorEl = document.getElementById('forgot-error');
        const successEl = document.getElementById('forgot-success');
        errorEl.classList.remove('show');
        successEl.classList.remove('show');

        btn.disabled = true;
        const orig = btn.innerHTML;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Resending...';

        try {
            const { error } = await supabaseClient.auth.signInWithOtp({
                email: pendingResetEmail,
                options: { shouldCreateUser: false },
            });
            if (error) throw error;
            showAuthSuccess('forgot-success', 'A new code has been sent.');
            otpInputs.forEach(i => i.value = '');
            otpInputs[0].focus();
        } catch (err) {
            showAuthError('forgot-error', err.message || 'Could not resend.');
        } finally {
            btn.disabled = false;
            btn.innerHTML = orig;
        }
    });

    // ═══════════════════════════════════════════════════════
    // SET PASSWORD (recovery flow only now)
    // ═══════════════════════════════════════════════════════
    document.getElementById('setpw-form').addEventListener('submit', async (e) => {
        e.preventDefault();
        const pwd = document.getElementById('setpw-password').value;
        const confirm = document.getElementById('setpw-confirm').value;
        const btn = document.getElementById('setpw-submit');
        const errEl = document.getElementById('setpw-error');
        const successEl = document.getElementById('setpw-success');
        errEl.classList.remove('show');
        successEl.classList.remove('show');

        if (pwd.length < 6) {
            errEl.textContent = 'Password must be at least 6 characters.';
            errEl.classList.add('show');
            return;
        }
        if (pwd !== confirm) {
            errEl.textContent = 'Passwords do not match.';
            errEl.classList.add('show');
            return;
        }

        btn.disabled = true;
        const orig = btn.innerHTML;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Saving...';

        try {
            const { error } = await supabaseClient.auth.updateUser({ password: pwd });
            if (error) throw error;

            successEl.textContent = 'Password set! Signing you out so you can log in with it...';
            successEl.classList.add('show');

            setTimeout(async () => {
                try { await supabaseClient.auth.signOut(); } catch (_) {}
                try {
                    window.history.replaceState({}, document.title, window.location.pathname);
                } catch (_) {}
                showLoginScreen('Your password is set. Sign in with your email and new password.');
            }, 1200);
        } catch (err) {
            console.error('Set password error:', err);
            errEl.textContent = err.message || 'Could not save password. Please try again.';
            errEl.classList.add('show');
            btn.disabled = false;
            btn.innerHTML = orig;
        }
    });

    document.getElementById('forgot-newpass-form').addEventListener('submit', async (e) => {
        e.preventDefault();
        const newPass = document.getElementById('new-password').value;
        const confirmPass = document.getElementById('confirm-password').value;
        const btn = document.getElementById('forgot-reset-btn');
        const errorEl = document.getElementById('forgot-error');
        errorEl.classList.remove('show');

        if (newPass.length < 6) {
            showAuthError('forgot-error', 'Password must be at least 6 characters.');
            return;
        }
        if (newPass !== confirmPass) {
            showAuthError('forgot-error', 'Passwords do not match.');
            return;
        }

        btn.disabled = true;
        const orig = btn.innerHTML;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Updating...';

        try {
            const { error } = await supabaseClient.auth.updateUser({ password: newPass });
            if (error) throw error;

            await supabaseClient.auth.signOut();

            forgotCard.style.display = 'none';
            loginCard.style.display = '';
            resetForgotForms();
            showAuthSuccess('login-success', 'Password updated! Please sign in with your new password.');
        } catch (err) {
            console.error('Password update error:', err);
            showAuthError('forgot-error', err.message || 'Could not update password.');
        } finally {
            btn.disabled = false;
            btn.innerHTML = orig;
        }
    });

    // ═══════════════════════════════════════════════════════
    // AUTHENTICATED
    // ═══════════════════════════════════════════════════════
    function onAuthenticated(me) {
        currentStaff = me;
        updateUserWidget(me);
        applyRoleVisibility(me.permissions || []);
        hideAuthGate();
        bootAdmin();
    }

    function updateUserWidget(me) {
        const widget = document.getElementById('user-widget');
        const initials = (me.full_name || me.email || '?')
            .split(/\s+/).map(s => s[0]).join('').slice(0, 2).toUpperCase();
        document.getElementById('user-avatar').textContent = initials || '?';
        document.getElementById('user-name').textContent = me.full_name || me.email;
        document.getElementById('user-role').textContent = me.role;
        widget.style.display = '';
    }

    function applyRoleVisibility(permissions) {
        const has = (perm) => permissions.includes('*') || permissions.includes(perm);

        document.querySelectorAll('#admin-tabs button[data-perm]').forEach(btn => {
            const needed = btn.dataset.perm;
            const ok = has(needed);
            btn.style.display = ok ? '' : 'none';
        });

        const activeBtn = document.querySelector('#admin-tabs button.active');
        if (activeBtn && activeBtn.style.display === 'none') {
            const firstVisible = document.querySelector('#admin-tabs button:not([style*="display: none"])');
            if (firstVisible) {
                firstVisible.click();
            }
        }
    }

    document.getElementById('logout-btn').addEventListener('click', async () => {
        if (!confirm('Sign out of the admin panel?')) return;
        try {
            await supabaseClient.auth.signOut();
        } catch (e) {}
        try { stopPolling(); } catch (e) {}
        showLoginScreen('You have been signed out.');
    });

    // ═══════════════════════════════════════════════════════
    // API ENDPOINTS
    // ═══════════════════════════════════════════════════════
    const API = {
        products: '/api/products',
        categories: '/api/categories',
        orders: '/api/orders',
        stats: '/api/stats',
        banners: '/api/v1/banners',
        topProducts: '/api/top-products',
        deliveryAreas: '/api/delivery-areas',
        valueRules: '/api/admin/delivery-rules',
        peakSettings: '/api/admin/peak-settings',
        loyaltySettings: '/api/admin/loyalty-settings',
        itemSurchargeRules: '/api/admin/item-surcharge-rules',
        staff: '/api/staff',
        auditLog: '/api/admin/audit-log',
    };

    const toastEl = document.getElementById('toast-admin');
    const toastMsg = document.getElementById('toast-admin-msg');
    let toastTimer = null;

    function showToast(msg, icon = 'fa-solid fa-check-circle') {
        toastMsg.textContent = msg;
        toastEl.querySelector('i').className = icon;
        toastEl.classList.add('show');
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => toastEl.classList.remove('show'), 3000);
    }

    let products = [];
    let categories = [];
    let orders = [];
    let banners = [];
    let topProducts = [];
    let staffList = [];
    let currentTab = 'products';
    let currentPage = 1;
    let pageSize = 12;
    let totalPages = 1;
    let currentView = 'all';
    let previousOrderCount = 0;
    let pollInterval = null;
    let lastPollTimestamp = null;
    let cancelOrderId = null;
    let auditOffset = 0;
    let auditLimit = 50;

    let deliveryAreas = [];
    let areaMap = null;
    let areaMapModal = null;
    let areaDrawControl = null;
    let areaDrawnPolygon = null;
    let areaEditLayer = null;
    let areaMapLayers = [];
    let areaEditId = null;

    let autocompleteTimeout = null;

    let topProductsCache = null;
    let topProductsCacheTime = 0;
    const TOP_PRODUCTS_CACHE_TTL = 30000;

    const tabs = document.querySelectorAll('#admin-tabs button');
    const panels = {
        products: document.getElementById('panel-products'),
        inventory: document.getElementById('panel-inventory'),
        orders: document.getElementById('panel-orders'),
        categories: document.getElementById('panel-categories'),
        banners: document.getElementById('panel-banners'),
        topproducts: document.getElementById('panel-topproducts'),
        areas: document.getElementById('panel-areas'),
        deliveryrules: document.getElementById('panel-deliveryrules'),
        staff: document.getElementById('panel-staff'),
        audit: document.getElementById('panel-audit'),
    };
    const productsBody = document.getElementById('products-table-body');
    const inventoryBody = document.getElementById('inventory-table-body');
    const ordersBody = document.getElementById('orders-table-body');
    const categoriesBody = document.getElementById('categories-table-body');
    const bannerGrid = document.getElementById('banner-grid');
    const pageInfo = document.getElementById('page-info');
    const ordersBadge = document.getElementById('orders-badge');
    const topProductsBody = document.getElementById('top-products-body');
    const staffBody = document.getElementById('staff-table-body');
    const auditBody = document.getElementById('audit-table-body');

    const productSearch = document.getElementById('product-search');
    const orderSearch = document.getElementById('order-search');
    const orderDateFilter = document.getElementById('order-date-filter');
    const orderFilterBtn = document.getElementById('order-filter-btn');
    const orderClearFilter = document.getElementById('order-clear-filter');
    const staffSearch = document.getElementById('staff-search');
    const auditResourceFilter = document.getElementById('audit-resource-filter');

    const productModal = document.getElementById('product-modal');
    const productForm = document.getElementById('product-form');
    const productId = document.getElementById('product-id');
    const pName = document.getElementById('p-name');
    const pDescription = document.getElementById('p-description');
    const pPrice = document.getElementById('p-price');
    const pStock = document.getElementById('p-stock');
    const pCategory = document.getElementById('p-category');
    const pEmoji = document.getElementById('p-emoji');
    const pImage = document.getElementById('p-image');
    const pTagcolor = document.getElementById('p-tagcolor');
    const pIsMainItem = document.getElementById('p-is-main-item');
    const pWeight = document.getElementById('p-weight');
    const pIsBulky = document.getElementById('p-is-bulky');
    const productModalTitle = document.getElementById('product-modal-title');
    const productModalSave = document.getElementById('product-modal-save');

    const categoryModal = document.getElementById('category-modal');
    const categoryForm = document.getElementById('category-form');
    const categoryId = document.getElementById('category-id');
    const cName = document.getElementById('c-name');
    const categoryModalTitle = document.getElementById('category-modal-title');

    const orderModal = document.getElementById('order-modal');
    const orderDetailBody = document.getElementById('order-detail-body');

    const refreshBtn = document.getElementById('refresh-data');

    const bannerModal = document.getElementById('banner-modal');
    const bannerForm = document.getElementById('banner-form');
    const bannerIdEl = document.getElementById('banner-id');
    const bannerModalTitle = document.getElementById('banner-modal-title');
    const bannerModalSave = document.getElementById('banner-modal-save');
    const bannerPreviewModal = document.getElementById('banner-preview-modal');
    const previewContent = document.getElementById('banner-preview-content');

    const areaModal = document.getElementById('area-modal');
    const areaForm = document.getElementById('area-form');
    const areaId = document.getElementById('area-id');
    const areaName = document.getElementById('area-name');
    const areaFee = document.getElementById('area-fee');
    const areaModalTitle = document.getElementById('area-modal-title');
    const areaModalSave = document.getElementById('area-modal-save');
    const polygonStatus = document.getElementById('polygon-status');
    const areaCount = document.getElementById('area-count');
    const areasBody = document.getElementById('areas-table-body');

    const cancelModal = document.getElementById('cancel-modal');
    const cancelForm = document.getElementById('cancel-form');
    const cancelOrderIdInput = document.getElementById('cancel-order-id');
    const cancelReason = document.getElementById('cancel-reason');

    const locationSearch = document.getElementById('location-search');
    const autocompleteDropdown = document.getElementById('autocomplete-dropdown');

    // [FEATURE] Staff modal refs
    const staffPasswordGroup = document.getElementById('staff-password-group');
    const staffPasswordInput = document.getElementById('s-password');

    // ─── FETCH HELPER ───
    async function fetchJSON(url, options = {}) {
        const res = await apiFetch(url, options);
        if (!res.ok) throw new Error(`HTTP ${res.status}: ${res.statusText}`);
        if (res.status === 204) return null;
        return res.json();
    }

    function getProductImage(product) {
        return product.image || null;
    }

    function humanizeStatus(status) {
        const map = {
            'pending': 'Pending',
            'awaiting_payment': 'Awaiting Payment',
            'paid': 'Paid',
            'confirmed': 'Confirmed',
            'completed': 'Completed',
            'cancelled': 'Cancelled',
            'preparing': 'Preparing',
            'ready': 'Ready'
        };
        return map[status] || status;
    }

    function paymentBadgeHtml(paymentMethod) {
        const method = (paymentMethod || 'online').toLowerCase();
        if (method === 'offline') {
            return '<span class="payment-badge offline" title="Customer will pay at the counter">💵 Pay at counter</span>';
        }
        return '<span class="payment-badge online" title="Paid online via Monnify">💳 Online</span>';
    }

    // ═══════════════════════════════════════════════════════
    // BOOT
    // ═══════════════════════════════════════════════════════
    let adminBooted = false;
    function bootAdmin() {
        if (adminBooted) return;
        adminBooted = true;

        loadBannerProductOptions();
        loadAllData(true).then(() => {
            startPolling();
            setTimeout(() => { try { initAreaMap(); } catch (e) {} }, 500);
        });
    }

    async function loadAllData(forceTopProducts = false) {
        try {
            await Promise.all([
                loadProducts(),
                loadCategories(),
                loadOrders(),
                loadStats(),
                loadBanners(),
                loadTopProducts(forceTopProducts)
            ]);
        } catch (err) {
            console.error('Load error:', err);
            showToast('Error loading data: ' + err.message, 'fa-solid fa-exclamation-circle');
        }
    }

    async function loadProducts() {
        const data = await fetchJSON(API.products);
        products = data;
        renderProducts();
        renderInventory();
        return data;
    }

    async function loadCategories() {
        const data = await fetchJSON(API.categories);
        categories = data;
        renderCategories();
        populateCategorySelect();
        populateBannerCategorySelect();
        return data;
    }

    async function loadOrders() {
        const data = await fetchJSON(API.orders);
        orders = data;
        previousOrderCount = orders.length;
        if (orders.length > 0 && orders[0].created_at) {
            lastPollTimestamp = orders[0].created_at;
        } else {
            lastPollTimestamp = new Date().toISOString();
        }
        renderOrders();
        updateOrdersBadge(orders.length);
        return data;
    }

    async function loadStats() {
        const data = await fetchJSON(API.stats);
        document.getElementById('stat-products').textContent = data.totalProducts || 0;
        document.getElementById('stat-categories').textContent = data.totalCategories || 0;
        document.getElementById('stat-orders').textContent = data.totalOrders || 0;
        document.getElementById('stat-revenue').textContent = '₦' + (data.totalRevenue || 0).toLocaleString();
        return data;
    }

    async function loadTopProducts(force = false) {
        const now = Date.now();
        if (!force && topProductsCache && (now - topProductsCacheTime) < TOP_PRODUCTS_CACHE_TTL) {
            topProducts = topProductsCache;
            renderTopProducts();
            return topProducts;
        }
        try {
            const data = await fetchJSON(API.topProducts);
            topProducts = data || [];
            topProductsCache = topProducts;
            topProductsCacheTime = now;
            renderTopProducts();
        } catch (err) {
            console.error('Error loading top products:', err);
            topProducts = [];
            renderTopProducts();
        }
        return topProducts;
    }

    async function loadDeliveryAreas() {
        try {
            const data = await fetchJSON(API.deliveryAreas);
            deliveryAreas = data || [];
            renderAreasTable();
            updateAreaMap();
            if (areaCount) areaCount.textContent = deliveryAreas.length;
        } catch (err) {
            console.error('Error loading delivery areas:', err);
            showToast('Failed to load delivery areas: ' + err.message, 'fa-solid fa-exclamation-circle');
            deliveryAreas = [];
            renderAreasTable();
            if (areaCount) areaCount.textContent = '0';
        }
    }

    // ═══════════════════════════════════════════════════════
    // RENDER: PRODUCTS
    // ═══════════════════════════════════════════════════════
    function renderProducts() {
        const search = productSearch.value.toLowerCase().trim();
        let filtered = products;
        if (search) {
            filtered = filtered.filter(p =>
                p.name.toLowerCase().includes(search) ||
                (p.tag || '').toLowerCase().includes(search)
            );
        }
        if (filtered.length === 0) {
            productsBody.innerHTML =
                `<tr><td colspan="6" class="empty-state"><i class="fa-regular fa-box"></i> No products found</td></tr>`;
            return;
        }
        productsBody.innerHTML = filtered.map(p => {
            const imageUrl = getProductImage(p);
            const safeImageUrl = escapeHtml(imageUrl);
            const imageHtml = imageUrl ?
                `<img src="${safeImageUrl}" alt="${escapeHtml(p.name)}" class="thumb" loading="lazy" onerror="this.style.display='none'">` :
                `<span class="emoji">${escapeHtml(p.emoji) || '🍽️'}</span>`;
            const deliveryInfo = [];
            if (p.is_main_item !== undefined) deliveryInfo.push(p.is_main_item ? 'Main' : 'Side');
            if (p.is_bulky) deliveryInfo.push('📦 Bulky');
            if (p.weight_kg !== undefined) deliveryInfo.push(`${p.weight_kg}kg`);
            const deliveryDisplay = deliveryInfo.length > 0 ? deliveryInfo.join(' · ') : 'Default';
            return `
                <tr>
                    <td>
                        <div class="product-cell">
                            ${imageHtml}
                            <div class="info">
                                <div class="name">${escapeHtml(p.name)}</div>
                                <div class="tag">${escapeHtml(p.tag)}</div>
                            </div>
                        </div>
                    </td>
                    <td><span class="tag-preview ${escapeHtml(p.tagColor) || 'primary'}">${escapeHtml(p.tag)}</span></td>
                    <td>₦${Number(p.price).toLocaleString()}</td>
                    <td><span class="stock-badge ${p.stock > 10 ? 'in-stock' : p.stock > 0 ? 'low-stock' : 'out-of-stock'}">${p.stock}</span></td>
                    <td style="font-size:0.7rem;color:var(--text-muted);">${escapeHtml(deliveryDisplay)}</td>
                    <td style="text-align:right;">
                        <div class="actions-cell" style="justify-content:flex-end;">
                            <button class="btn btn-primary btn-sm edit-product" data-id="${p.id}"><i class="fa-regular fa-pen-to-square"></i></button>
                            <button class="btn btn-danger btn-sm delete-product" data-id="${p.id}"><i class="fa-regular fa-trash-can"></i></button>
                        </div>
                    </td>
                </tr>
            `;
        }).join('');
        document.querySelectorAll('.edit-product').forEach(btn => {
            btn.addEventListener('click', () => editProduct(btn.dataset.id));
        });
        document.querySelectorAll('.delete-product').forEach(btn => {
            btn.addEventListener('click', () => deleteProduct(btn.dataset.id));
        });
    }

    function renderInventory() {
        const view = document.querySelector('#inventory-view-toggle .active')?.dataset.view || 'all';
        let filtered = [...products];
        if (view === 'low') filtered = filtered.filter(p => p.stock > 0 && p.stock <= 10);
        if (view === 'out') filtered = filtered.filter(p => p.stock === 0);
        if (filtered.length === 0) {
            inventoryBody.innerHTML =
                `<tr><td colspan="5" class="empty-state"><i class="fa-regular fa-box"></i> No items in this view</td></tr>`;
            return;
        }
        inventoryBody.innerHTML = filtered.map(p => {
            const status = p.stock > 10 ? 'In Stock' : p.stock > 0 ? 'Low Stock' : 'Out of Stock';
            const badge = p.stock > 10 ? 'in-stock' : p.stock > 0 ? 'low-stock' : 'out-of-stock';
            const imageUrl = getProductImage(p);
            const safeImageUrl = escapeHtml(imageUrl);
            const imageHtml = imageUrl ?
                `<img src="${safeImageUrl}" alt="${escapeHtml(p.name)}" class="thumb" loading="lazy" onerror="this.style.display='none'">` :
                `<span class="emoji">${escapeHtml(p.emoji) || '🍽️'}</span>`;
            return `
                <tr>
                    <td>
                        <div class="product-cell">
                            ${imageHtml}
                            <div class="info"><div class="name">${escapeHtml(p.name)}</div></div>
                        </div>
                    </td>
                    <td>${escapeHtml(p.tag)}</td>
                    <td><strong>${p.stock}</strong></td>
                    <td><span class="stock-badge ${badge}">${status}</span></td>
                    <td style="text-align:right;">
                        <button class="btn btn-primary btn-sm adjust-stock" data-id="${p.id}" data-current="${p.stock}"><i class="fa-solid fa-pen"></i> Adjust</button>
                    </td>
                </tr>
            `;
        }).join('');
        document.querySelectorAll('.adjust-stock').forEach(btn => {
            btn.addEventListener('click', () => adjustStock(btn.dataset.id, parseInt(btn.dataset.current)));
        });
    }

    function renderOrders() {
        const search = orderSearch.value.toLowerCase().trim();
        const dateFilter = orderDateFilter.value;
        let filtered = orders;
        if (search) {
            filtered = filtered.filter(o =>
                (o.customer_name || '').toLowerCase().includes(search) ||
                (o.payment_reference || '').toLowerCase().includes(search)
            );
        }
        if (dateFilter) {
            const selectedDate = new Date(dateFilter + 'T00:00:00');
            filtered = filtered.filter(o => {
                if (!o.created_at) return false;
                const orderDate = new Date(o.created_at);
                return orderDate.toDateString() === selectedDate.toDateString();
            });
        }
        if (filtered.length === 0) {
            ordersBody.innerHTML =
                `<tr><td colspan="9" class="empty-state"><i class="fa-regular fa-receipt"></i> No orders found</td></tr>`;
            return;
        }
        ordersBody.innerHTML = filtered.map(o => {
            const itemCount = Array.isArray(o.items) ? o.items.reduce((s, i) => s + (i.qty || 0), 0) : 0;
            const createdAt = o.created_at ? new Date(o.created_at).toLocaleString() : '-';
            const status = o.status || 'pending';
            const isCancelled = status === 'cancelled';
            const isCompleted = status === 'completed';
            const isAwaitingPayment = status === 'awaiting_payment';
            const isConfirmed = status === 'confirmed';
            const reason = o.cancellation_reason || '';
            const safeReason = escapeHtml(reason);
            const reasonHtml = reason ? `<span class="cancellation-reason" title="${safeReason}">📝 ${safeReason}</span>` : '';

            let actionButtons = '';
            if (!isCancelled && !isCompleted) {
                if (isAwaitingPayment) {
                    actionButtons += `<button class="btn btn-success btn-sm confirm-order" data-id="${o.id}" title="Customer paid at counter"><i class="fa-solid fa-money-bill-wave"></i> Mark Paid</button>`;
                } else if (isConfirmed) {
                    actionButtons += `<button class="btn btn-success btn-sm complete-order" data-id="${o.id}" title="Customer received their order"><i class="fa-solid fa-flag-checkered"></i> Complete</button>`;
                } else {
                    actionButtons += `<button class="btn btn-success btn-sm confirm-order" data-id="${o.id}"><i class="fa-solid fa-check"></i> Confirm</button>`;
                }
                actionButtons += `<button class="btn btn-danger btn-sm cancel-order" data-id="${o.id}"><i class="fa-solid fa-ban"></i> Cancel</button>`;
            }

            return `
                <tr>
                    <td><code style="background:#f0f0f0;padding:2px 8px;border-radius:4px;font-size:0.75rem;">${escapeHtml(o.payment_reference || o.id)}</code></td>
                    <td><strong>${escapeHtml(o.customer_name) || 'N/A'}</strong></td>
                    <td>${escapeHtml(o.customer_phone) || 'N/A'}</td>
                    <td>${itemCount} items</td>
                    <td>₦${Number(o.total || 0).toLocaleString()}</td>
                    <td>${paymentBadgeHtml(o.payment_method)}</td>
                    <td>
                        <span class="status-badge ${status}">${humanizeStatus(status)}</span>
                        ${reasonHtml}
                    </td>
                    <td style="font-size:0.75rem;color:var(--text-muted);">${createdAt}</td>
                    <td style="text-align:right;">
                        <div class="actions-cell" style="justify-content:flex-end;">
                            ${actionButtons}
                            <button class="btn btn-primary btn-sm view-order" data-id="${o.id}"><i class="fa-regular fa-eye"></i></button>
                        </div>
                    </td>
                </tr>
            `;
        }).join('');

        document.querySelectorAll('.confirm-order').forEach(btn => {
            btn.addEventListener('click', (e) => confirmOrder(e.currentTarget.dataset.id));
        });
        document.querySelectorAll('.complete-order').forEach(btn => {
            btn.addEventListener('click', (e) => completeOrder(e.currentTarget.dataset.id));
        });
        document.querySelectorAll('.cancel-order').forEach(btn => {
            btn.addEventListener('click', (e) => openCancelModal(e.currentTarget.dataset.id));
        });
        document.querySelectorAll('.view-order').forEach(btn => {
            btn.addEventListener('click', (e) => viewOrder(e.currentTarget.dataset.id));
        });
    }

    async function confirmOrder(id) {
        const order = orders.find(o => o.id == id);
        if (!order) return;
        const status = order.status;
        const isOffline = status === 'awaiting_payment';
        const promptMsg = isOffline
            ? 'Mark this order as paid? Customer paid at the counter. Stock will be reduced.'
            : 'Confirm this order? Stock will be reduced.';
        if (!confirm(promptMsg)) return;

        const btn = document.querySelector(`.confirm-order[data-id="${id}"]`);
        const originalHtml = btn ? btn.innerHTML : '';
        if (btn) { btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Working...'; btn.disabled = true; }
        try {
            let response;
            if (isOffline) {
                response = await apiFetch(`/api/orders/${id}/confirm-offline`, { method: 'POST' });
            } else {
                response = await apiFetch(`/api/orders/${id}/status`, {
                    method: 'PATCH',
                    body: JSON.stringify({ status: 'confirmed' })
                });
            }
            if (!response.ok) {
                const e = await response.json().catch(() => ({}));
                throw new Error(e.detail || 'Confirmation failed');
            }
            showToast(isOffline ? '✅ Payment recorded!' : '✅ Order confirmed!');
            await Promise.all([loadOrders(), loadStats(), loadProducts(), loadTopProducts(true)]);
            if (currentTab === 'orders') renderOrders();
            if (currentTab === 'inventory') renderInventory();
        } catch (err) {
            console.error('Confirm error:', err);
            showToast('❌ Error: ' + err.message, 'fa-solid fa-exclamation-circle');
        } finally {
            if (btn) { btn.innerHTML = originalHtml; btn.disabled = false; }
        }
    }

    async function completeOrder(id) {
        if (!confirm('Mark this order as completed? Customer has received their order.')) return;
        const btn = document.querySelector(`.complete-order[data-id="${id}"]`);
        const originalHtml = btn ? btn.innerHTML : '';
        if (btn) { btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Working...'; btn.disabled = true; }
        try {
            const response = await apiFetch(`/api/orders/${id}/status`, {
                method: 'PATCH',
                body: JSON.stringify({ status: 'completed' })
            });
            if (!response.ok) {
                const e = await response.json().catch(() => ({}));
                throw new Error(e.detail || 'Completion failed');
            }
            showToast('✅ Order completed!');
            await Promise.all([loadOrders(), loadStats()]);
            if (currentTab === 'orders') renderOrders();
        } catch (err) {
            console.error('Complete error:', err);
            showToast('❌ Error: ' + err.message, 'fa-solid fa-exclamation-circle');
        } finally {
            if (btn) { btn.innerHTML = originalHtml; btn.disabled = false; }
        }
    }

    async function submitCancel(e) {
        e.preventDefault();
        const id = cancelOrderIdInput.value;
        const reason = cancelReason.value.trim();
        if (!reason) {
            showToast('Please provide a cancellation reason.', 'fa-solid fa-exclamation-circle');
            cancelReason.focus();
            return;
        }
        const submitBtn = cancelForm.querySelector('button[type="submit"]');
        const originalHtml = submitBtn.innerHTML;
        submitBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Cancelling...';
        submitBtn.disabled = true;
        try {
            const response = await apiFetch(`/api/orders/${id}/status`, {
                method: 'PATCH',
                body: JSON.stringify({ status: 'cancelled', reason })
            });
            if (!response.ok) {
                const e = await response.json().catch(() => ({}));
                throw new Error(e.detail || 'Cancellation failed');
            }
            showToast('✅ Order cancelled.');
            closeCancelModal();
            await Promise.all([loadOrders(), loadStats(), loadProducts(), loadTopProducts(true)]);
            if (currentTab === 'orders') renderOrders();
            if (currentTab === 'inventory') renderInventory();
        } catch (err) {
            console.error('Cancel error:', err);
            showToast('❌ Error: ' + err.message, 'fa-solid fa-exclamation-circle');
        } finally {
            submitBtn.innerHTML = originalHtml;
            submitBtn.disabled = false;
        }
    }

    function openCancelModal(orderId) {
        cancelOrderId = orderId;
        cancelOrderIdInput.value = orderId;
        cancelReason.value = '';
        cancelModal.classList.add('open');
        setTimeout(() => cancelReason.focus(), 100);
    }
    function closeCancelModal() {
        cancelModal.classList.remove('open');
        cancelOrderId = null;
    }

    function renderTopProducts() {
        if (!topProductsBody) return;
        if (topProducts.length === 0) {
            topProductsBody.innerHTML =
                `<tr><td colspan="4" class="empty-state"><i class="fa-regular fa-chart-simple"></i> No sales data yet</td></tr>`;
            return;
        }
        let html = '';
        topProducts.forEach((item, index) => {
            html += `
                <tr>
                    <td><strong>${index + 1}</strong></td>
                    <td>
                        <div class="product-cell">
                            <span class="emoji">${escapeHtml(item.emoji) || '🍽️'}</span>
                            <div class="info"><div class="name">${escapeHtml(item.name)}</div></div>
                        </div>
                    </td>
                    <td>${item.quantity_sold}</td>
                    <td>₦${Number(item.revenue || 0).toLocaleString()}</td>
                </tr>
            `;
        });
        topProductsBody.innerHTML = html;
    }

    function renderCategories() {
        if (categories.length === 0) {
            categoriesBody.innerHTML =
                `<tr><td colspan="3" class="empty-state"><i class="fa-regular fa-tags"></i> No categories</td></tr>`;
            return;
        }
        categoriesBody.innerHTML = categories.map(c => {
            const count = products.filter(p => p.tag === c.name).length;
            return `
                <tr>
                    <td><strong>${escapeHtml(c.name)}</strong></td>
                    <td>${count} products</td>
                    <td style="text-align:right;">
                        <button class="btn btn-primary btn-sm edit-category" data-id="${c.id}"><i class="fa-regular fa-pen-to-square"></i></button>
                        <button class="btn btn-danger btn-sm delete-category" data-id="${c.id}"><i class="fa-regular fa-trash-can"></i></button>
                    </td>
                </tr>
            `;
        }).join('');
        document.querySelectorAll('.edit-category').forEach(btn => btn.addEventListener('click', () => editCategory(btn.dataset.id)));
        document.querySelectorAll('.delete-category').forEach(btn => btn.addEventListener('click', () => deleteCategory(btn.dataset.id)));
    }

    function populateCategorySelect() {
        const current = pCategory.value;
        pCategory.innerHTML = '<option value="">Select category...</option>' +
            categories.map(c => `<option value="${escapeHtml(c.name)}">${escapeHtml(c.name)}</option>`).join('');
        if (current) pCategory.value = current;
    }

    // ═══════════════════════════════════════════════════════
    // PRODUCT CRUD
    // ═══════════════════════════════════════════════════════
    function openProductModal(product = null) {
        productModal.classList.add('open');
        if (product) {
            productModalTitle.textContent = 'Edit Product';
            productModalSave.textContent = 'Update';
            productId.value = product.id;
            pName.value = product.name;
            pDescription.value = product.description || '';
            pPrice.value = product.price;
            pStock.value = product.stock || 0;
            pCategory.value = product.tag || '';
            pEmoji.value = product.emoji || '';
            pImage.value = product.image || '';
            pTagcolor.value = product.tagColor || 'primary';
            pIsMainItem.value = product.is_main_item !== undefined ? String(product.is_main_item) : 'true';
            pWeight.value = product.weight_kg || 0.5;
            pIsBulky.checked = product.is_bulky || false;
        } else {
            productModalTitle.textContent = 'Add Product';
            productModalSave.textContent = 'Save';
            productId.value = '';
            productForm.reset();
            pIsMainItem.value = 'true';
            pWeight.value = 0.5;
        }
    }
    function closeProductModal() { productModal.classList.remove('open'); }

    async function saveProduct(e) {
        e.preventDefault();
        const data = {
            name: pName.value.trim(),
            description: pDescription.value.trim(),
            price: parseFloat(pPrice.value),
            stock: parseInt(pStock.value) || 0,
            tag: pCategory.value,
            emoji: pEmoji.value.trim() || '🍽️',
            image: pImage.value.trim() || '',
            tagColor: pTagcolor.value,
            is_main_item: pIsMainItem.value === 'true',
            weight_kg: parseFloat(pWeight.value) || 0.5,
            is_bulky: pIsBulky.checked,
        };
        const id = productId.value;
        try {
            if (id) {
                await fetchJSON(`${API.products}/${id}`, { method: 'PUT', body: JSON.stringify(data) });
                showToast('Product updated successfully!');
            } else {
                await fetchJSON(API.products, { method: 'POST', body: JSON.stringify(data) });
                showToast('Product added successfully!');
            }
            closeProductModal();
            await loadAllData(true);
        } catch (err) {
            showToast('Error: ' + err.message, 'fa-solid fa-exclamation-circle');
        }
    }

    async function editProduct(id) {
        const product = products.find(p => p.id == id);
        if (product) openProductModal(product);
    }

    async function deleteProduct(id) {
        if (!confirm('Delete this product permanently?')) return;
        try {
            await apiFetch(`${API.products}/${id}`, { method: 'DELETE' });
            showToast('Product deleted.');
            await loadAllData(true);
        } catch (err) {
            showToast('Error: ' + err.message, 'fa-solid fa-exclamation-circle');
        }
    }

    async function adjustStock(id, current) {
        const newStock = prompt(`Current stock: ${current}\nEnter new stock quantity:`, current);
        if (newStock === null) return;
        const val = parseInt(newStock);
        if (isNaN(val) || val < 0) {
            showToast('Please enter a valid number.', 'fa-solid fa-exclamation-circle');
            return;
        }
        try {
            await fetchJSON(`${API.products}/${id}`, {
                method: 'PUT',
                body: JSON.stringify({ stock: val })
            });
            showToast(`Stock updated to ${val}`);
            await loadAllData(true);
        } catch (err) {
            showToast('Error: ' + err.message, 'fa-solid fa-exclamation-circle');
        }
    }

    // ═══════════════════════════════════════════════════════
    // CATEGORY CRUD
    // ═══════════════════════════════════════════════════════
    function openCategoryModal(category = null) {
        categoryModal.classList.add('open');
        if (category) {
            categoryModalTitle.textContent = 'Edit Category';
            categoryId.value = category.id;
            cName.value = category.name;
        } else {
            categoryModalTitle.textContent = 'Add Category';
            categoryId.value = '';
            cName.value = '';
        }
    }
    function closeCategoryModal() { categoryModal.classList.remove('open'); }

    async function saveCategory(e) {
        e.preventDefault();
        const data = { name: cName.value.trim() };
        const id = categoryId.value;
        try {
            if (id) {
                await fetchJSON(`${API.categories}/${id}`, { method: 'PUT', body: JSON.stringify(data) });
                showToast('Category updated!');
            } else {
                await fetchJSON(API.categories, { method: 'POST', body: JSON.stringify(data) });
                showToast('Category added!');
            }
            closeCategoryModal();
            await loadAllData(true);
        } catch (err) {
            showToast('Error: ' + err.message, 'fa-solid fa-exclamation-circle');
        }
    }
    async function editCategory(id) {
        const cat = categories.find(c => c.id == id);
        if (cat) openCategoryModal(cat);
    }
    async function deleteCategory(id) {
        const cat = categories.find(c => c.id == id);
        const count = products.filter(p => p.tag === cat?.name).length;
        if (count > 0) {
            if (!confirm(`Category "${cat?.name}" has ${count} products. Delete anyway?`)) return;
        } else {
            if (!confirm(`Delete category "${cat?.name}"?`)) return;
        }
        try {
            await apiFetch(`${API.categories}/${id}`, { method: 'DELETE' });
            showToast('Category deleted.');
            await loadAllData(true);
        } catch (err) {
            showToast('Error: ' + err.message, 'fa-solid fa-exclamation-circle');
        }
    }

    // ═══════════════════════════════════════════════════════
    // ORDERS VIEW
    // ═══════════════════════════════════════════════════════
    async function viewOrder(id) {
        try {
            const data = await fetchJSON(`${API.orders}/${id}`);
            const createdAt = data.created_at ? new Date(data.created_at).toLocaleString() : '-';
            const itemsHtml = (data.items || []).map(item => `
                <div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px dashed #f0f0f0;">
                    <span>${escapeHtml(item.name)} × ${item.qty}</span>
                    <span>₦${Number(item.price * item.qty).toLocaleString()}</span>
                </div>
            `).join('') || '<p style="color:var(--text-muted);">No items</p>';

            const reasonHtml = data.cancellation_reason ?
                `<p><strong>Cancellation Reason:</strong> ${escapeHtml(data.cancellation_reason)}</p>` : '';

            const paymentMethod = (data.payment_method || 'online').toLowerCase();
            const paymentLabel = paymentMethod === 'offline' ? '💵 Pay at counter' : '💳 Online (Monnify)';

            let offlineNotice = '';
            if (paymentMethod === 'offline' && data.status === 'awaiting_payment') {
                offlineNotice = `
                    <div class="offline-notice">
                        💵 <strong>Collect ₦${Number(data.total || 0).toLocaleString()} at the counter</strong>
                        — customer chose to pay on arrival (${escapeHtml((data.delivery_method || 'pickup').toUpperCase())}).
                        Click "Mark Paid" in the orders list once payment is received.
                    </div>
                `;
            }

            orderDetailBody.innerHTML = `
                ${offlineNotice}
                <div style="margin-bottom:16px;">
                    <p><strong>Order ID:</strong> <code>${escapeHtml(data.payment_reference || data.id)}</code></p>
                    <p><strong>Customer:</strong> ${escapeHtml(data.customer_name) || 'N/A'}</p>
                    <p><strong>Email:</strong> ${escapeHtml(data.customer_email) || 'N/A'}</p>
                    <p><strong>Phone:</strong> ${escapeHtml(data.customer_phone) || 'N/A'}</p>
                    <p><strong>Status:</strong> <span class="status-badge ${escapeHtml(data.status || 'pending')}">${humanizeStatus(data.status || 'pending')}</span></p>
                    <p><strong>Payment:</strong> ${paymentLabel}</p>
                    ${reasonHtml}
                    <p><strong>Date:</strong> ${createdAt}</p>
                    ${data.delivery_method ? `<p><strong>Delivery Method:</strong> ${escapeHtml(data.delivery_method)}</p>` : ''}
                    ${data.delivery_address ? `<p><strong>Address:</strong> ${escapeHtml(data.delivery_address)}</p>` : ''}
                    ${data.preferred_time ? `<p><strong>Preferred Time:</strong> ${escapeHtml(data.preferred_time)}</p>` : ''}
                    ${data.order_notes ? `<p><strong>Notes:</strong> ${escapeHtml(data.order_notes)}</p>` : ''}
                    ${data.monnify_transaction_ref ? `<p><strong>Monnify Ref:</strong> ${escapeHtml(data.monnify_transaction_ref)}</p>` : ''}
                    ${data.delivery_fee ? `<p><strong>Delivery Fee:</strong> ₦${Number(data.delivery_fee).toLocaleString()}</p>` : ''}
                </div>
                <hr style="border-color:#f0f0f0;margin:12px 0;">
                <div style="margin:12px 0;">
                    <strong>Items:</strong>
                    ${itemsHtml}
                </div>
                <hr style="border-color:#f0f0f0;margin:12px 0;">
                <div style="display:flex;justify-content:space-between;font-size:1.1rem;font-weight:800;padding-top:8px;">
                    <span>Total</span>
                    <span style="color:var(--primary);">₦${Number(data.total || 0).toLocaleString()}</span>
                </div>
            `;
            orderModal.classList.add('open');
        } catch (err) {
            showToast('Error loading order: ' + err.message, 'fa-solid fa-exclamation-circle');
        }
    }
    function closeOrderModal() { orderModal.classList.remove('open'); }

    function updateOrdersBadge(count) {
        if (ordersBadge) {
            if (count > 0) {
                ordersBadge.textContent = count;
                ordersBadge.classList.add('show');
            } else {
                ordersBadge.classList.remove('show');
            }
        }
    }

    async function pollOrders() {
        if (!currentStaff) return;
        try {
            const url = `${API.orders}?since=${encodeURIComponent(lastPollTimestamp)}`;
            const response = await apiFetch(url);
            if (!response.ok) return;
            const newOrders = await response.json();
            if (newOrders.length > 0) {
                const newOrder = newOrders[0];
                const status = newOrder.status;
                if (status === 'awaiting_payment') {
                    showToast(
                        `💵 New offline order #${newOrder.payment_reference || newOrder.id} from ${newOrder.customer_name || 'Customer'} — collect at counter`,
                        'fa-solid fa-money-bill-wave'
                    );
                } else if (status === 'paid') {
                    showToast(
                        `💳 New paid order #${newOrder.payment_reference || newOrder.id} from ${newOrder.customer_name || 'Customer'}`,
                        'fa-solid fa-bell'
                    );
                }
                orders = [...newOrders, ...orders];
                previousOrderCount = orders.length;
                updateOrdersBadge(orders.length);
                document.getElementById('stat-orders').textContent = orders.length;
                if (currentTab === 'orders') renderOrders();
                const latestTimestamp = newOrders[0]?.created_at;
                lastPollTimestamp = latestTimestamp || new Date().toISOString();
            }
        } catch (err) {
            console.warn('Order poll failed:', err);
        }
    }

    function startPolling() {
        if (pollInterval) clearInterval(pollInterval);
        pollInterval = setInterval(pollOrders, 30000);
    }
    function stopPolling() {
        if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
    }

    // ═══════════════════════════════════════════════════════
    // BANNERS
    // ═══════════════════════════════════════════════════════
    async function loadBanners() {
        try {
            const params = new URLSearchParams({
                limit: pageSize,
                offset: (currentPage - 1) * pageSize
            });
            if (currentView === 'active') params.append('is_active', 'true');
            else if (currentView === 'hero') params.append('is_hero', 'true');
            else if (currentView === 'featured') params.append('is_featured', 'true');
            const response = await apiFetch(`${API.banners}?${params}`);
            const data = await response.json();
            banners = data.banners || [];
            totalPages = Math.ceil((data.total || 0) / pageSize);
            renderBanners();
            updatePagination();
        } catch (error) {
            console.error('Error loading banners:', error);
            showToast('Failed to load banners', 'fa-solid fa-exclamation-circle');
        }
    }

    function renderBanners() {
        if (!bannerGrid) return;
        if (banners.length === 0) {
            bannerGrid.innerHTML = `
                <div class="empty-state" style="grid-column:1/-1;">
                    <i class="fa-regular fa-images"></i>
                    <p>No banners found. Create your first banner!</p>
                </div>
            `;
            return;
        }
        bannerGrid.innerHTML = banners.map((banner, index) => {
            const safeImageUrl = /^https?:\/\//.test(banner.image_url || '') || /^\//.test(banner.image_url || '')
                ? banner.image_url
                : '/images/default-banner.jpg';
            return `
            <div class="banner-card" draggable="true" data-index="${index}" data-id="${banner.id}">
                <div class="banner-image" style="background-image: url('${safeImageUrl}');">
                    ${banner.badge_text ? `<span class="badge" style="background:${banner.badge_color || '#E53935'};">${escapeHtml(banner.badge_text)}</span>` : ''}
                    <span class="status-badge ${banner.is_active ? 'active' : 'inactive'}">${banner.is_active ? 'Active' : 'Inactive'}</span>
                </div>
                <div class="banner-content">
                    <div class="title">${escapeHtml(banner.title)}</div>
                    ${banner.subtitle ? `<div class="subtitle">${escapeHtml(banner.subtitle)}</div>` : ''}
                    ${banner.description ? `<p style="font-size:0.85rem;color:var(--text-muted);margin:8px 0;">${escapeHtml(banner.description)}</p>` : ''}
                    <div class="meta">
                        <span class="tag">Position: ${banner.position || 0}</span>
                        <span class="tag">Order: ${banner.display_order || 0}</span>
                        ${banner.discount_value ? `<span class="tag" style="background:#dcfce7;color:#166534;">${banner.discount_value}% OFF</span>` : ''}
                    </div>
                </div>
                <div class="banner-actions">
                    <button class="btn btn-primary btn-sm edit-banner" data-id="${banner.id}"><i class="fa-regular fa-pen-to-square"></i></button>
                    <button class="btn btn-outline btn-sm preview-banner" data-id="${banner.id}"><i class="fa-regular fa-eye"></i></button>
                    <button class="btn btn-success btn-sm toggle-banner" data-id="${banner.id}"><i class="fa-solid ${banner.is_active ? 'fa-pause' : 'fa-play'}"></i></button>
                    <button class="btn btn-outline btn-sm duplicate-banner" data-id="${banner.id}"><i class="fa-regular fa-copy"></i></button>
                    <button class="btn btn-danger btn-sm delete-banner" data-id="${banner.id}"><i class="fa-regular fa-trash-can"></i></button>
                    <span class="drag-handle" style="margin-left:auto;"><i class="fa-solid fa-grip-vertical"></i></span>
                </div>
            </div>
            `;
        }).join('');
        setupBannerDragDrop();
        document.querySelectorAll('.edit-banner').forEach(btn => btn.addEventListener('click', () => openBannerModal(btn.dataset.id)));
        document.querySelectorAll('.preview-banner').forEach(btn => btn.addEventListener('click', () => previewBanner(btn.dataset.id)));
        document.querySelectorAll('.toggle-banner').forEach(btn => btn.addEventListener('click', () => toggleBanner(btn.dataset.id)));
        document.querySelectorAll('.duplicate-banner').forEach(btn => btn.addEventListener('click', () => duplicateBanner(btn.dataset.id)));
        document.querySelectorAll('.delete-banner').forEach(btn => btn.addEventListener('click', () => deleteBanner(btn.dataset.id)));
    }

    function setupBannerDragDrop() {
        let dragStartIndex = null;
        let dragEndIndex = null;
        const grid = bannerGrid;
        grid.querySelectorAll('.banner-card').forEach(card => {
            card.addEventListener('dragstart', (e) => {
                dragStartIndex = parseInt(card.dataset.index);
                card.classList.add('dragging');
                e.dataTransfer.effectAllowed = 'move';
            });
            card.addEventListener('dragend', () => {
                card.classList.remove('dragging');
                if (dragStartIndex !== null && dragEndIndex !== null && dragStartIndex !== dragEndIndex) {
                    reorderBanners(dragStartIndex, dragEndIndex);
                }
                dragStartIndex = null;
                dragEndIndex = null;
                grid.querySelectorAll('.banner-card').forEach(c => c.classList.remove('drag-over'));
            });
            card.addEventListener('dragover', (e) => {
                e.preventDefault();
                e.dataTransfer.dropEffect = 'move';
                card.classList.add('drag-over');
                dragEndIndex = parseInt(card.dataset.index);
            });
            card.addEventListener('dragleave', () => card.classList.remove('drag-over'));
        });
    }

    async function reorderBanners(fromIndex, toIndex) {
        const bannerIds = banners.map(b => b.id);
        const [removed] = bannerIds.splice(fromIndex, 1);
        bannerIds.splice(toIndex, 0, removed);
        try {
            await apiFetch(`${API.banners}/reorder`, {
                method: 'PATCH',
                body: JSON.stringify(bannerIds)
            });
            showToast('Banners reordered successfully!');
            loadBanners();
        } catch (error) {
            showToast('Failed to reorder banners', 'fa-solid fa-exclamation-circle');
        }
    }

    function updatePagination() {
        pageInfo.textContent = `Page ${currentPage} of ${totalPages || 1}`;
        document.getElementById('prev-page').disabled = currentPage <= 1;
        document.getElementById('next-page').disabled = currentPage >= totalPages;
    }

    function openBannerModal(bannerId = null) {
        const modal = document.getElementById('banner-modal');
        const form = document.getElementById('banner-form');
        form.reset();
        if (bannerId) {
            const banner = banners.find(b => b.id == bannerId);
            if (banner) {
                bannerModalTitle.textContent = 'Edit Banner';
                bannerIdEl.value = banner.id;
                document.getElementById('b-title').value = banner.title || '';
                document.getElementById('b-subtitle').value = banner.subtitle || '';
                document.getElementById('b-description').value = banner.description || '';
                document.getElementById('b-badge').value = banner.badge_text || '';
                document.getElementById('b-badge-color').value = banner.badge_color || '#E53935';
                document.getElementById('b-bg-color').value = banner.background_color || '#fff3ed';
                document.getElementById('b-text-color').value = banner.text_color || '#1e1e1e';
                document.getElementById('b-image').value = banner.image_url || '';
                document.getElementById('b-cta-text').value = banner.cta_text || '';
                document.getElementById('b-cta-link').value = banner.cta_link || '';
                document.getElementById('b-cta-type').value = banner.cta_type || 'button';
                document.getElementById('b-product-id').value = banner.product_id || '';
                document.getElementById('b-discount-type').value = banner.discount_type || '';
                document.getElementById('b-discount-value').value = banner.discount_value || '';
                document.getElementById('b-position').value = banner.position || 0;
                document.getElementById('b-display-order').value = banner.display_order || 0;
                document.getElementById('b-is-active').checked = banner.is_active;
                document.getElementById('b-is-hero').checked = banner.is_hero;
                document.getElementById('b-is-featured').checked = banner.is_featured;
                document.getElementById('b-meta').value = JSON.stringify(banner.meta_data || {}, null, 2);
                if (banner.start_date) document.getElementById('b-start-date').value = banner.start_date.slice(0, 16);
                if (banner.end_date) document.getElementById('b-end-date').value = banner.end_date.slice(0, 16);
                const categorySelect = document.getElementById('b-categories');
                if (banner.categories) {
                    Array.from(categorySelect.options).forEach(opt => {
                        opt.selected = banner.categories.includes(parseInt(opt.value));
                    });
                }
                bannerModalSave.textContent = 'Update Banner';
            }
        } else {
            bannerModalTitle.textContent = 'Add Banner';
            bannerIdEl.value = '';
            document.getElementById('b-is-active').checked = true;
            bannerModalSave.textContent = 'Create Banner';
        }
        modal.classList.add('open');
    }
    function closeBannerModal() {
        document.getElementById('banner-modal').classList.remove('open');
        document.getElementById('banner-form').reset();
    }

    async function saveBanner(e) {
        e.preventDefault();
        const id = bannerIdEl.value;
        const categories = Array.from(document.getElementById('b-categories').selectedOptions)
            .map(opt => parseInt(opt.value));
        const data = {
            title: document.getElementById('b-title').value,
            subtitle: document.getElementById('b-subtitle').value,
            description: document.getElementById('b-description').value,
            image_url: document.getElementById('b-image').value,
            badge_text: document.getElementById('b-badge').value,
            badge_color: document.getElementById('b-badge-color').value,
            background_color: document.getElementById('b-bg-color').value,
            text_color: document.getElementById('b-text-color').value,
            cta_text: document.getElementById('b-cta-text').value,
            cta_link: document.getElementById('b-cta-link').value,
            cta_type: document.getElementById('b-cta-type').value,
            product_id: parseInt(document.getElementById('b-product-id').value) || null,
            discount_type: document.getElementById('b-discount-type').value || null,
            discount_value: parseInt(document.getElementById('b-discount-value').value) || null,
            position: parseInt(document.getElementById('b-position').value) || 0,
            display_order: parseInt(document.getElementById('b-display-order').value) || 0,
            is_active: document.getElementById('b-is-active').checked,
            is_hero: document.getElementById('b-is-hero').checked,
            is_featured: document.getElementById('b-is-featured').checked,
            start_date: document.getElementById('b-start-date').value || null,
            end_date: document.getElementById('b-end-date').value || null,
            meta_data: JSON.parse(document.getElementById('b-meta').value || '{}'),
            categories: categories
        };
        try {
            const url = id ? `${API.banners}/${id}` : API.banners;
            const method = id ? 'PUT' : 'POST';
            const response = await apiFetch(url, { method, body: JSON.stringify(data) });
            if (response.ok) {
                showToast(id ? 'Banner updated successfully!' : 'Banner created successfully!');
                closeBannerModal();
                loadBanners();
            } else {
                const error = await response.json();
                showToast('Error: ' + (error.message || 'Failed to save banner'), 'fa-solid fa-exclamation-circle');
            }
        } catch (error) {
            showToast('Failed to save banner', 'fa-solid fa-exclamation-circle');
        }
    }

    async function toggleBanner(id) {
        try {
            const response = await apiFetch(`${API.banners}/${id}/toggle`, { method: 'PATCH' });
            if (response.ok) { showToast('Banner toggled!'); loadBanners(); }
        } catch (error) { showToast('Failed to toggle banner', 'fa-solid fa-exclamation-circle'); }
    }
    async function duplicateBanner(id) {
        try {
            const response = await apiFetch(`${API.banners}/${id}/duplicate`, { method: 'POST' });
            if (response.ok) { showToast('Banner duplicated!'); loadBanners(); }
        } catch (error) { showToast('Failed to duplicate banner', 'fa-solid fa-exclamation-circle'); }
    }
    async function deleteBanner(id) {
        if (!confirm('Are you sure you want to delete this banner?')) return;
        try {
            await apiFetch(`${API.banners}/${id}`, { method: 'DELETE' });
            showToast('Banner deleted!');
            loadBanners();
        } catch (error) { showToast('Failed to delete banner', 'fa-solid fa-exclamation-circle'); }
    }
    async function previewBanner(id) {
        const banner = banners.find(b => b.id == id);
        if (!banner) return;
        previewContent.innerHTML = `
            <div class="banner-preview" style="background:${banner.background_color || '#fff3ed'};color:${banner.text_color || '#1e1e1e'};">
                ${banner.badge_text ? `<span class="preview-badge" style="background:${banner.badge_color || '#E53935'};color:white;">${escapeHtml(banner.badge_text)}</span>` : ''}
                <h1>${escapeHtml(banner.title)}</h1>
                ${banner.subtitle ? `<p style="font-weight:700;font-size:1.2rem;">${escapeHtml(banner.subtitle)}</p>` : ''}
                ${banner.description ? `<p>${escapeHtml(banner.description)}</p>` : ''}
                ${banner.cta_text ? `<div class="preview-cta" style="background:${banner.badge_color || '#E53935'};color:white;">${escapeHtml(banner.cta_text)}${banner.discount_value ? ` (${banner.discount_type === 'percentage' ? banner.discount_value + '% OFF' : '₦' + banner.discount_value + ' OFF'})` : ''}</div>` : ''}
            </div>
        `;
        bannerPreviewModal.classList.add('open');
    }
    function populateBannerCategorySelect() {
        const select = document.getElementById('b-categories');
        select.innerHTML = categories.map(c => `<option value="${escapeHtml(c.id)}">${escapeHtml(c.name)}</option>`).join('');
    }
    async function loadBannerProductOptions() {
        try {
            const response = await apiFetch(API.products);
            const items = await response.json();
            const select = document.getElementById('b-product-id');
            select.innerHTML = `<option value="">Select product...</option>` +
                items.map(p => `<option value="${escapeHtml(p.id)}">${escapeHtml(p.name)} (₦${p.price})</option>`).join('');
        } catch (error) {
            console.error('Error loading products for banner:', error);
        }
    }

    // ═══════════════════════════════════════════════════════
    // DELIVERY AREAS
    // ═══════════════════════════════════════════════════════
    function renderAreasTable() {
        if (!areasBody) return;
        if (deliveryAreas.length === 0) {
            areasBody.innerHTML = `
                <tr><td colspan="5" class="empty-state">
                    <i class="fa-regular fa-map-location-dot"></i>
                    <p>No delivery areas defined yet. Click "Add Area" to create one.</p>
                </td></tr>
            `;
            return;
        }
        areasBody.innerHTML = deliveryAreas.map(area => {
            const id = area.id || area.area_id;
            const name = area.name || area.area_name;
            const fee = area.fee || area.area_fee;
            const createdAt = area.created_at || area.area_created_at;
            return `
                <tr>
                    <td><code style="background:#f0f0f0;padding:2px 8px;border-radius:4px;font-size:0.75rem;">${escapeHtml(id)}</code></td>
                    <td><strong>${escapeHtml(name)}</strong></td>
                    <td>₦${Number(fee).toLocaleString()}</td>
                    <td style="font-size:0.75rem;color:var(--text-muted);">${createdAt ? new Date(createdAt).toLocaleDateString() : '-'}</td>
                    <td style="text-align:right;">
                        <div class="actions-cell" style="justify-content:flex-end;">
                            <button class="btn btn-primary btn-sm edit-area" data-id="${id}"><i class="fa-regular fa-pen-to-square"></i></button>
                            <button class="btn btn-danger btn-sm delete-area" data-id="${id}"><i class="fa-regular fa-trash-can"></i></button>
                        </div>
                    </td>
                </tr>
            `;
        }).join('');
        document.querySelectorAll('.edit-area').forEach(btn => btn.addEventListener('click', () => openAreaModal(btn.dataset.id)));
        document.querySelectorAll('.delete-area').forEach(btn => btn.addEventListener('click', () => deleteArea(btn.dataset.id)));
    }

    function initAreaMap() {
        const mapContainer = document.getElementById('delivery-area-map');
        if (!mapContainer) return;
        const center = [6.5244, 3.3792];
        const zoom = 12;
        if (!areaMap) {
            areaMap = L.map(mapContainer, { center, zoom, zoomControl: true });
            L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
                attribution: '&copy; OpenStreetMap contributors', maxZoom: 19,
            }).addTo(areaMap);
        } else {
            areaMap.setView(center, zoom);
        }
    }

    function updateAreaMap() {
        if (!areaMap) return;
        areaMapLayers.forEach(layer => { if (areaMap.hasLayer(layer)) areaMap.removeLayer(layer); });
        areaMapLayers = [];
        deliveryAreas.forEach(area => {
            try {
                const polygonData = area.polygon || area.area_polygon;
                if (!polygonData) return;
                const geojson = typeof polygonData === 'string' ? JSON.parse(polygonData) : polygonData;
                const name = area.name || area.area_name;
                const fee = area.fee || area.area_fee;
                const layer = L.geoJSON(geojson, {
                    style: { color: '#E53935', weight: 3, opacity: 0.8, fillColor: '#E53935', fillOpacity: 0.15 },
                    onEachFeature: (feature, l) => {
                        l.bindPopup(`<strong>${escapeHtml(name)}</strong><br>Fee: ₦${Number(fee).toLocaleString()}<br>ID: ${escapeHtml(area.id)}`);
                    }
                });
                areaMap.addLayer(layer);
                areaMapLayers.push(layer);
            } catch (err) {
                console.warn('Error rendering area:', err);
            }
        });
        if (areaMapLayers.length > 0) {
            const group = L.featureGroup(areaMapLayers);
            areaMap.fitBounds(group.getBounds().pad(0.1));
        } else {
            areaMap.setView([6.5244, 3.3792], 12);
        }
    }

    function initAreaModalMap() {
        const mapContainer = document.getElementById('delivery-area-map-modal');
        if (!mapContainer) return;
        const center = [6.5244, 3.3792];
        const zoom = 12;
        if (!areaMapModal) {
            areaMapModal = L.map(mapContainer, { center, zoom, zoomControl: true });
            L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
                attribution: '&copy; OpenStreetMap contributors', maxZoom: 19,
            }).addTo(areaMapModal);
            areaDrawControl = new L.Control.Draw({
                draw: {
                    polygon: {
                        allowIntersection: false, showArea: true,
                        shapeOptions: { color: '#E53935', weight: 3, opacity: 0.8, fillColor: '#E53935', fillOpacity: 0.2 }
                    },
                    polyline: false, rectangle: false, circle: false, circlemarker: false, marker: false,
                },
                edit: { featureGroup: new L.FeatureGroup(), remove: true }
            });
            areaMapModal.addControl(areaDrawControl);
            areaMapModal.on(L.Draw.Event.CREATED, function(event) {
                const layer = event.layer;
                if (areaDrawnPolygon) areaMapModal.removeLayer(areaDrawnPolygon);
                areaDrawnPolygon = layer;
                areaMapModal.addLayer(layer);
                const coords = layer.getLatLngs()[0];
                if (coords && coords.length >= 4) {
                    polygonStatus.textContent = `Polygon drawn (${coords.length} points)`;
                    polygonStatus.style.color = '#22c55e';
                }
            });
            areaMapModal.on(L.Draw.Event.DELETED, function() {
                if (areaDrawnPolygon) { areaMapModal.removeLayer(areaDrawnPolygon); areaDrawnPolygon = null; }
                polygonStatus.textContent = 'None';
                polygonStatus.style.color = 'var(--text-dark)';
            });
            areaMapModal.on(L.Draw.Event.EDITED, function(event) {
                event.layers.eachLayer(function(layer) {
                    if (areaDrawnPolygon) areaMapModal.removeLayer(areaDrawnPolygon);
                    areaDrawnPolygon = layer;
                    areaMapModal.addLayer(layer);
                    const coords = layer.getLatLngs()[0];
                    if (coords && coords.length >= 4) {
                        polygonStatus.textContent = `Polygon edited (${coords.length} points)`;
                        polygonStatus.style.color = '#22c55e';
                    }
                });
            });
        } else {
            areaMapModal.setView(center, zoom);
            if (areaDrawnPolygon) { areaMapModal.removeLayer(areaDrawnPolygon); areaDrawnPolygon = null; }
            if (areaEditLayer) { areaMapModal.removeLayer(areaEditLayer); areaEditLayer = null; }
            polygonStatus.textContent = 'None';
            polygonStatus.style.color = 'var(--text-dark)';
        }
    }

    function getPolygonGeoJSON() {
        if (!areaDrawnPolygon) return null;
        try {
            const feature = areaDrawnPolygon.toGeoJSON();
            return feature && feature.geometry ? feature.geometry : null;
        } catch (err) {
            console.error('Polygon conversion failed:', err);
            return null;
        }
    }

    function setPolygonFromGeoJSON(geojson) {
        if (!areaMapModal || !geojson) return;
        try {
            if (areaDrawnPolygon) { areaMapModal.removeLayer(areaDrawnPolygon); areaDrawnPolygon = null; }
            if (areaEditLayer) { areaMapModal.removeLayer(areaEditLayer); areaEditLayer = null; }
            const layer = L.geoJSON(geojson, {
                style: { color: '#E53935', weight: 3, opacity: 0.8, fillColor: '#E53935', fillOpacity: 0.2 }
            });
            areaEditLayer = layer;
            areaMapModal.addLayer(layer);
            areaMapModal.fitBounds(layer.getBounds().pad(0.1));
            layer.eachLayer(l => {
                areaDrawnPolygon = l;
                const coords = l.getLatLngs()[0];
                if (coords && coords.length >= 4) {
                    polygonStatus.textContent = `Polygon loaded (${coords.length} points)`;
                    polygonStatus.style.color = '#22c55e';
                }
            });
        } catch (err) {
            showToast('Error loading polygon: ' + err.message, 'fa-solid fa-exclamation-circle');
        }
    }

    async function searchLocations(query) {
        if (!query || query.length < 2) { autocompleteDropdown.classList.remove('show'); return; }
        const loadingEl = autocompleteDropdown.querySelector('.autocomplete-loading');
        const emptyEl = autocompleteDropdown.querySelector('.autocomplete-empty');
        loadingEl.style.display = 'block';
        emptyEl.style.display = 'none';
        autocompleteDropdown.classList.add('show');
        try {
            const url = `https://nominatim.openstreetmap.org/search?q=${encodeURIComponent(query)}&format=json&limit=8&addressdetails=1`;
            const response = await fetch(url, { headers: { 'User-Agent': 'HotPortionGrill/1.0' } });
            if (!response.ok) throw new Error('Search failed');
            const data = await response.json();
            loadingEl.style.display = 'none';
            autocompleteDropdown.querySelectorAll('.autocomplete-item').forEach(el => el.remove());
            if (!data || data.length === 0) { emptyEl.style.display = 'block'; return; }
            emptyEl.style.display = 'none';
            data.forEach(result => {
                const item = document.createElement('div');
                item.className = 'autocomplete-item';
                const displayName = result.display_name || result.name || 'Unknown location';
                const subText = result.address
                    ? `${result.address.city || result.address.town || result.address.village || ''}, ${result.address.country || ''}`
                    : '';
                item.innerHTML = `
                    <span>${highlightText(escapeHtml(displayName), query)}</span>
                    ${subText ? `<span class="sub-text">${highlightText(escapeHtml(subText), query)}</span>` : ''}
                `;
                item.addEventListener('click', function() {
                    const lat = parseFloat(result.lat);
                    const lng = parseFloat(result.lon);
                    if (areaMapModal && !isNaN(lat) && !isNaN(lng)) {
                        areaMapModal.setView([lat, lng], 14);
                        const marker = L.marker([lat, lng]).addTo(areaMapModal);
                        setTimeout(() => { areaMapModal.removeLayer(marker); }, 5000);
                    }
                    locationSearch.value = displayName;
                    autocompleteDropdown.classList.remove('show');
                });
                autocompleteDropdown.appendChild(item);
            });
        } catch (err) {
            loadingEl.style.display = 'none';
            emptyEl.style.display = 'block';
            emptyEl.textContent = 'Error searching locations';
        }
    }

    function highlightText(text, query) {
        if (!query || !text) return text;
        const regex = new RegExp(`(${query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`, 'gi');
        return text.replace(regex, '<span class="highlight">$1</span>');
    }

    function openAreaModal(areaIdToEdit = null) {
        areaEditId = areaIdToEdit;
        const modal = areaModal;
        const form = areaForm;
        form.reset();
        polygonStatus.textContent = 'None';
        polygonStatus.style.color = 'var(--text-dark)';
        locationSearch.value = '';
        autocompleteDropdown.classList.remove('show');
        if (areaMapModal) {
            if (areaDrawnPolygon) { areaMapModal.removeLayer(areaDrawnPolygon); areaDrawnPolygon = null; }
            if (areaEditLayer) { areaMapModal.removeLayer(areaEditLayer); areaEditLayer = null; }
            areaMapModal.setView([6.5244, 3.3792], 12);
        }
        if (areaIdToEdit) {
            const area = deliveryAreas.find(a => a.id == areaIdToEdit || a.area_id == areaIdToEdit);
            if (area) {
                areaModalTitle.textContent = 'Edit Delivery Area';
                areaModalSave.textContent = 'Update Area';
                areaId.value = area.id || area.area_id;
                areaName.value = area.name || area.area_name;
                areaFee.value = area.fee || area.area_fee;
                const polygonData = area.polygon || area.area_polygon;
                if (polygonData) {
                    const geojson = typeof polygonData === 'string' ? JSON.parse(polygonData) : polygonData;
                    setTimeout(() => { if (areaMapModal) setPolygonFromGeoJSON(geojson); }, 300);
                }
            }
        } else {
            areaModalTitle.textContent = 'Add Delivery Area';
            areaModalSave.textContent = 'Create Area';
            areaId.value = '';
            areaName.value = '';
            areaFee.value = '';
        }
        modal.classList.add('open');
        setTimeout(() => {
            initAreaModalMap();
            if (areaMapModal) areaMapModal.invalidateSize();
        }, 100);
    }

    function closeAreaModal() {
        areaModal.classList.remove('open');
        if (areaDrawnPolygon && areaMapModal) { areaMapModal.removeLayer(areaDrawnPolygon); areaDrawnPolygon = null; }
        if (areaEditLayer && areaMapModal) { areaMapModal.removeLayer(areaEditLayer); areaEditLayer = null; }
        areaEditId = null;
        autocompleteDropdown.classList.remove('show');
    }

    async function saveArea(e) {
        e.preventDefault();
        const id = areaId.value;
        const name = areaName.value.trim();
        const fee = parseInt(areaFee.value);
        if (!name) { showToast('Please enter an area name.', 'fa-solid fa-exclamation-circle'); return; }
        if (isNaN(fee) || fee < 0) { showToast('Please enter a valid fee.', 'fa-solid fa-exclamation-circle'); return; }
        const polygon = getPolygonGeoJSON();
        if (!polygon) { showToast('Please draw a polygon on the map.', 'fa-solid fa-exclamation-circle'); return; }
        const data = { name, fee, polygon };
        try {
            const response = id
                ? await apiFetch(`${API.deliveryAreas}/${id}`, { method: 'PUT', body: JSON.stringify(data) })
                : await apiFetch(API.deliveryAreas, { method: 'POST', body: JSON.stringify(data) });
            let result = null;
            const ct = response.headers.get('content-type');
            if (ct && ct.includes('application/json')) result = await response.json();
            if (!response.ok) throw new Error(result?.detail || result?.message || 'Failed to save area');
            showToast(id ? 'Area updated!' : 'Area created!');
            closeAreaModal();
            await loadDeliveryAreas();
        } catch (err) {
            showToast('Error: ' + err.message, 'fa-solid fa-exclamation-circle');
        }
    }

    async function deleteArea(id) {
        const area = deliveryAreas.find(a => a.id == id || a.area_id == id);
        const name = area?.name || area?.area_name || 'Unknown';
        if (!area) return;
        if (!confirm(`Delete delivery area "${name}"? This action cannot be undone.`)) return;
        try {
            const response = await apiFetch(`${API.deliveryAreas}/${id}`, { method: 'DELETE' });
            if (!response.ok) {
                const result = await response.json().catch(() => ({}));
                throw new Error(result.detail || 'Failed to delete area');
            }
            showToast('Area deleted!');
            await loadDeliveryAreas();
        } catch (err) {
            showToast('Error: ' + err.message, 'fa-solid fa-exclamation-circle');
        }
    }

    // ═══════════════════════════════════════════════════════
    // DELIVERY RULES
    // ═══════════════════════════════════════════════════════
    async function loadValueRules() {
        try {
            const data = await fetchJSON(API.valueRules);
            renderValueRules(data);
        } catch (err) {
            document.getElementById('value-rules-body').innerHTML =
                `<tr><td colspan="7" class="empty-state">Failed to load rules</td></tr>`;
        }
    }
    function renderValueRules(rules) {
        const tbody = document.getElementById('value-rules-body');
        if (!rules || rules.length === 0) {
            tbody.innerHTML = `<tr><td colspan="7" class="empty-state">No value discount rules defined.</td></tr>`;
            return;
        }
        tbody.innerHTML = rules.map(r => `
            <tr>
                <td>₦${r.min_order_value}</td>
                <td>${r.max_order_value ? '₦' + r.max_order_value : '∞'}</td>
                <td>${r.fee_multiplier}</td>
                <td>₦${r.fee_discount}</td>
                <td>${r.free_delivery ? '✅' : '❌'}</td>
                <td>${escapeHtml(r.description) || '-'}</td>
                <td style="text-align:right;">
                    <button class="btn btn-primary btn-sm edit-value-rule" data-id="${r.id}"><i class="fa-regular fa-pen-to-square"></i></button>
                    <button class="btn btn-danger btn-sm delete-value-rule" data-id="${r.id}"><i class="fa-regular fa-trash-can"></i></button>
                </td>
            </tr>
        `).join('');
        document.querySelectorAll('.edit-value-rule').forEach(btn => btn.addEventListener('click', () => openValueRuleModal(btn.dataset.id)));
        document.querySelectorAll('.delete-value-rule').forEach(btn => btn.addEventListener('click', () => deleteValueRule(btn.dataset.id)));
    }

    async function loadPeakSettings() {
        try {
            const data = await fetchJSON(API.peakSettings);
            renderPeakSettings(data);
        } catch (err) {
            document.getElementById('peak-settings-body').innerHTML =
                `<tr><td colspan="6" class="empty-state">Failed to load peak settings</td></tr>`;
        }
    }
    function renderPeakSettings(settings) {
        const tbody = document.getElementById('peak-settings-body');
        if (!settings || settings.length === 0) {
            tbody.innerHTML = `<tr><td colspan="6" class="empty-state">No peak hour settings defined.</td></tr>`;
            return;
        }
        const days = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
        tbody.innerHTML = settings.map(s => `
            <tr>
                <td>${s.day_of_week !== null && s.day_of_week !== undefined ? days[s.day_of_week] : 'All Days'}</td>
                <td>${escapeHtml(s.start_time)}</td>
                <td>${escapeHtml(s.end_time)}</td>
                <td>₦${s.surcharge_amount}</td>
                <td>${s.is_active ? '✅' : '❌'}</td>
                <td style="text-align:right;">
                    <button class="btn btn-primary btn-sm edit-peak-setting" data-id="${s.id}"><i class="fa-regular fa-pen-to-square"></i></button>
                    <button class="btn btn-danger btn-sm delete-peak-setting" data-id="${s.id}"><i class="fa-regular fa-trash-can"></i></button>
                </td>
            </tr>
        `).join('');
        document.querySelectorAll('.edit-peak-setting').forEach(btn => btn.addEventListener('click', () => openPeakSettingModal(btn.dataset.id)));
        document.querySelectorAll('.delete-peak-setting').forEach(btn => btn.addEventListener('click', () => deletePeakSetting(btn.dataset.id)));
    }

    async function loadLoyaltySettings() {
        try {
            const data = await fetchJSON(API.loyaltySettings);
            renderLoyaltySettings(data);
        } catch (err) {
            document.getElementById('loyalty-settings-body').innerHTML =
                `<tr><td colspan="4" class="empty-state">Failed to load loyalty settings</td></tr>`;
        }
    }
    function renderLoyaltySettings(settings) {
        const tbody = document.getElementById('loyalty-settings-body');
        if (!settings || settings.length === 0) {
            tbody.innerHTML = `<tr><td colspan="4" class="empty-state">No loyalty settings defined.</td></tr>`;
            return;
        }
        tbody.innerHTML = settings.map(s => `
            <tr>
                <td>${s.min_orders}</td>
                <td>${s.discount_percentage}%</td>
                <td>${s.is_active ? '✅' : '❌'}</td>
                <td style="text-align:right;">
                    <button class="btn btn-primary btn-sm edit-loyalty-setting" data-id="${s.id}"><i class="fa-regular fa-pen-to-square"></i></button>
                    <button class="btn btn-danger btn-sm delete-loyalty-setting" data-id="${s.id}"><i class="fa-regular fa-trash-can"></i></button>
                </td>
            </tr>
        `).join('');
        document.querySelectorAll('.edit-loyalty-setting').forEach(btn => btn.addEventListener('click', () => openLoyaltySettingModal(btn.dataset.id)));
        document.querySelectorAll('.delete-loyalty-setting').forEach(btn => btn.addEventListener('click', () => deleteLoyaltySetting(btn.dataset.id)));
    }

    async function loadItemSurchargeRules() {
        try {
            const data = await fetchJSON(API.itemSurchargeRules);
            renderItemSurchargeRules(data);
        } catch (err) {
            document.getElementById('item-surcharge-body').innerHTML =
                `<tr><td colspan="7" class="empty-state">Failed to load item surcharge rules</td></tr>`;
        }
    }
    function renderItemSurchargeRules(rules) {
        const tbody = document.getElementById('item-surcharge-body');
        if (!rules || rules.length === 0) {
            tbody.innerHTML = `<tr><td colspan="7" class="empty-state">No item surcharge rules defined.</td></tr>`;
            return;
        }
        tbody.innerHTML = rules.map(r => `
            <tr>
                <td>${r.min_main_items ?? '∞'}</td>
                <td>${r.max_main_items ?? '∞'}</td>
                <td>₦${r.surcharge_amount}</td>
                <td>${r.weight_threshold_kg ?? '-'}</td>
                <td>${r.surcharge_per_kg ? '₦' + r.surcharge_per_kg : '-'}</td>
                <td>${escapeHtml(r.description) || '-'}</td>
                <td style="text-align:right;">
                    <button class="btn btn-primary btn-sm edit-item-surcharge" data-id="${r.id}"><i class="fa-regular fa-pen-to-square"></i></button>
                    <button class="btn btn-danger btn-sm delete-item-surcharge" data-id="${r.id}"><i class="fa-regular fa-trash-can"></i></button>
                </td>
            </tr>
        `).join('');
        document.querySelectorAll('.edit-item-surcharge').forEach(btn => btn.addEventListener('click', () => openItemSurchargeModal(btn.dataset.id)));
        document.querySelectorAll('.delete-item-surcharge').forEach(btn => btn.addEventListener('click', () => deleteItemSurchargeRule(btn.dataset.id)));
    }

    function openValueRuleModal(id = null) {
        const modal = document.getElementById('value-rule-modal');
        const form = document.getElementById('value-rule-form');
        form.reset();
        document.getElementById('value-rule-id').value = '';
        document.getElementById('value-rule-title').textContent = id ? 'Edit Value Discount Rule' : 'Add Value Discount Rule';
        if (id) {
            fetchJSON(`${API.valueRules}/${id}`).then(rule => {
                if (rule) {
                    document.getElementById('value-rule-id').value = rule.id;
                    document.getElementById('vr-min').value = rule.min_order_value;
                    document.getElementById('vr-max').value = rule.max_order_value || '';
                    document.getElementById('vr-multiplier').value = rule.fee_multiplier;
                    document.getElementById('vr-discount').value = rule.fee_discount;
                    document.getElementById('vr-free').checked = rule.free_delivery;
                    document.getElementById('vr-desc').value = rule.description || '';
                }
            }).catch(err => console.error(err));
        }
        modal.classList.add('open');
    }
    function closeValueRuleModal() { document.getElementById('value-rule-modal').classList.remove('open'); }
    async function saveValueRule(e) {
        e.preventDefault();
        const id = document.getElementById('value-rule-id').value;
        const data = {
            min_order_value: parseInt(document.getElementById('vr-min').value),
            max_order_value: document.getElementById('vr-max').value ? parseInt(document.getElementById('vr-max').value) : null,
            fee_multiplier: parseFloat(document.getElementById('vr-multiplier').value),
            fee_discount: parseInt(document.getElementById('vr-discount').value),
            free_delivery: document.getElementById('vr-free').checked,
            description: document.getElementById('vr-desc').value.trim() || null,
        };
        try {
            const url = id ? `${API.valueRules}/${id}` : API.valueRules;
            const method = id ? 'PUT' : 'POST';
            await fetchJSON(url, { method, body: JSON.stringify(data) });
            showToast(id ? 'Rule updated!' : 'Rule created!');
            closeValueRuleModal();
            loadValueRules();
        } catch (err) { showToast('Error: ' + err.message, 'fa-solid fa-exclamation-circle'); }
    }
    async function deleteValueRule(id) {
        if (!confirm('Delete this value discount rule?')) return;
        try {
            await apiFetch(`${API.valueRules}/${id}`, { method: 'DELETE' });
            showToast('Rule deleted.');
            loadValueRules();
        } catch (err) { showToast('Error: ' + err.message, 'fa-solid fa-exclamation-circle'); }
    }

    function openPeakSettingModal(id = null) {
        const modal = document.getElementById('peak-setting-modal');
        const form = document.getElementById('peak-setting-form');
        form.reset();
        document.getElementById('peak-setting-id').value = '';
        document.getElementById('peak-setting-title').textContent = id ? 'Edit Peak Setting' : 'Add Peak Setting';
        if (id) {
            fetchJSON(`${API.peakSettings}/${id}`).then(setting => {
                if (setting) {
                    document.getElementById('peak-setting-id').value = setting.id;
                    document.getElementById('ps-day').value = setting.day_of_week !== null ? setting.day_of_week : '';
                    document.getElementById('ps-start').value = setting.start_time;
                    document.getElementById('ps-end').value = setting.end_time;
                    document.getElementById('ps-amount').value = setting.surcharge_amount;
                    document.getElementById('ps-active').checked = setting.is_active;
                }
            }).catch(err => console.error(err));
        }
        modal.classList.add('open');
    }
    function closePeakSettingModal() { document.getElementById('peak-setting-modal').classList.remove('open'); }
    async function savePeakSetting(e) {
        e.preventDefault();
        const id = document.getElementById('peak-setting-id').value;
        const data = {
            day_of_week: document.getElementById('ps-day').value ? parseInt(document.getElementById('ps-day').value) : null,
            start_time: document.getElementById('ps-start').value,
            end_time: document.getElementById('ps-end').value,
            surcharge_amount: parseInt(document.getElementById('ps-amount').value),
            is_active: document.getElementById('ps-active').checked,
        };
        try {
            const url = id ? `${API.peakSettings}/${id}` : API.peakSettings;
            const method = id ? 'PUT' : 'POST';
            await fetchJSON(url, { method, body: JSON.stringify(data) });
            showToast(id ? 'Peak setting updated!' : 'Peak setting created!');
            closePeakSettingModal();
            loadPeakSettings();
        } catch (err) { showToast('Error: ' + err.message, 'fa-solid fa-exclamation-circle'); }
    }
    async function deletePeakSetting(id) {
        if (!confirm('Delete this peak setting?')) return;
        try {
            await apiFetch(`${API.peakSettings}/${id}`, { method: 'DELETE' });
            showToast('Peak setting deleted.');
            loadPeakSettings();
        } catch (err) { showToast('Error: ' + err.message, 'fa-solid fa-exclamation-circle'); }
    }

    function openLoyaltySettingModal(id = null) {
        const modal = document.getElementById('loyalty-setting-modal');
        const form = document.getElementById('loyalty-setting-form');
        form.reset();
        document.getElementById('loyalty-setting-id').value = '';
        document.getElementById('loyalty-setting-title').textContent = id ? 'Edit Loyalty Setting' : 'Add Loyalty Setting';
        if (id) {
            fetchJSON(`${API.loyaltySettings}/${id}`).then(setting => {
                if (setting) {
                    document.getElementById('loyalty-setting-id').value = setting.id;
                    document.getElementById('ls-min-orders').value = setting.min_orders;
                    document.getElementById('ls-discount-pct').value = setting.discount_percentage;
                    document.getElementById('ls-active').checked = setting.is_active;
                }
            }).catch(err => console.error(err));
        }
        modal.classList.add('open');
    }
    function closeLoyaltySettingModal() { document.getElementById('loyalty-setting-modal').classList.remove('open'); }
    async function saveLoyaltySetting(e) {
        e.preventDefault();
        const id = document.getElementById('loyalty-setting-id').value;
        const data = {
            min_orders: parseInt(document.getElementById('ls-min-orders').value),
            discount_percentage: parseInt(document.getElementById('ls-discount-pct').value),
            is_active: document.getElementById('ls-active').checked,
        };
        try {
            const url = id ? `${API.loyaltySettings}/${id}` : API.loyaltySettings;
            const method = id ? 'PUT' : 'POST';
            await fetchJSON(url, { method, body: JSON.stringify(data) });
            showToast(id ? 'Loyalty setting updated!' : 'Loyalty setting created!');
            closeLoyaltySettingModal();
            loadLoyaltySettings();
        } catch (err) { showToast('Error: ' + err.message, 'fa-solid fa-exclamation-circle'); }
    }
    async function deleteLoyaltySetting(id) {
        if (!confirm('Delete this loyalty setting?')) return;
        try {
            await apiFetch(`${API.loyaltySettings}/${id}`, { method: 'DELETE' });
            showToast('Loyalty setting deleted.');
            loadLoyaltySettings();
        } catch (err) { showToast('Error: ' + err.message, 'fa-solid fa-exclamation-circle'); }
    }

    function openItemSurchargeModal(id = null) {
        const modal = document.getElementById('item-surcharge-modal');
        const form = document.getElementById('item-surcharge-form');
        form.reset();
        document.getElementById('item-surcharge-id').value = '';
        document.getElementById('item-surcharge-title').textContent = id ? 'Edit Item Surcharge Rule' : 'Add Item Surcharge Rule';
        if (id) {
            fetchJSON(`${API.itemSurchargeRules}/${id}`).then(rule => {
                if (rule) {
                    document.getElementById('item-surcharge-id').value = rule.id;
                    document.getElementById('isr-min-items').value = rule.min_main_items || '';
                    document.getElementById('isr-max-items').value = rule.max_main_items || '';
                    document.getElementById('isr-amount').value = rule.surcharge_amount;
                    document.getElementById('isr-weight-threshold').value = rule.weight_threshold_kg || '';
                    document.getElementById('isr-per-kg').value = rule.surcharge_per_kg || 50;
                    document.getElementById('isr-desc').value = rule.description || '';
                }
            }).catch(err => console.error(err));
        }
        modal.classList.add('open');
    }
    function closeItemSurchargeModal() { document.getElementById('item-surcharge-modal').classList.remove('open'); }
    async function saveItemSurchargeRule(e) {
        e.preventDefault();
        const id = document.getElementById('item-surcharge-id').value;
        const data = {
            min_main_items: document.getElementById('isr-min-items').value ? parseInt(document.getElementById('isr-min-items').value) : null,
            max_main_items: document.getElementById('isr-max-items').value ? parseInt(document.getElementById('isr-max-items').value) : null,
            surcharge_amount: parseInt(document.getElementById('isr-amount').value),
            weight_threshold_kg: document.getElementById('isr-weight-threshold').value ? parseFloat(document.getElementById('isr-weight-threshold').value) : null,
            surcharge_per_kg: document.getElementById('isr-per-kg').value ? parseInt(document.getElementById('isr-per-kg').value) : null,
            description: document.getElementById('isr-desc').value.trim() || null,
        };
        try {
            const url = id ? `${API.itemSurchargeRules}/${id}` : API.itemSurchargeRules;
            const method = id ? 'PUT' : 'POST';
            await fetchJSON(url, { method, body: JSON.stringify(data) });
            showToast(id ? 'Item surcharge rule updated!' : 'Item surcharge rule created!');
            closeItemSurchargeModal();
            loadItemSurchargeRules();
        } catch (err) { showToast('Error: ' + err.message, 'fa-solid fa-exclamation-circle'); }
    }
    async function deleteItemSurchargeRule(id) {
        if (!confirm('Delete this item surcharge rule?')) return;
        try {
            await apiFetch(`${API.itemSurchargeRules}/${id}`, { method: 'DELETE' });
            showToast('Item surcharge rule deleted.');
            loadItemSurchargeRules();
        } catch (err) { showToast('Error: ' + err.message, 'fa-solid fa-exclamation-circle'); }
    }

    function switchSubTab(subtab) {
        document.querySelectorAll('#rules-sub-tabs button').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.subtab === subtab);
        });
        document.querySelectorAll('.sub-panel').forEach(panel => {
            panel.classList.toggle('active', panel.id === 'sub-' + subtab);
        });
    }

    function loadAllRules() {
        loadValueRules();
        loadPeakSettings();
        loadLoyaltySettings();
        loadItemSurchargeRules();
    }

    // ═══════════════════════════════════════════════════════
    // STAFF MANAGEMENT
    // ═══════════════════════════════════════════════════════
    async function loadStaff() {
        try {
            const data = await fetchJSON(API.staff);
            staffList = data || [];
            renderStaff();
        } catch (err) {
            console.error('Error loading staff:', err);
            staffBody.innerHTML = `<tr><td colspan="6" class="empty-state">Failed to load staff: ${escapeHtml(err.message)}</td></tr>`;
        }
    }

    function renderStaff() {
        const search = (staffSearch.value || '').toLowerCase().trim();
        let filtered = staffList;
        if (search) {
            filtered = filtered.filter(s =>
                (s.full_name || '').toLowerCase().includes(search) ||
                (s.email || '').toLowerCase().includes(search) ||
                (s.role || '').toLowerCase().includes(search)
            );
        }
        if (filtered.length === 0) {
            staffBody.innerHTML = `<tr><td colspan="6" class="empty-state"><i class="fa-regular fa-user"></i> No staff members found</td></tr>`;
            return;
        }
        staffBody.innerHTML = filtered.map(s => {
            const initials = (s.full_name || s.email || '?').split(/\s+/).map(x => x[0]).join('').slice(0, 2).toUpperCase();
            const joined = s.created_at ? new Date(s.created_at).toLocaleDateString() : '-';
            const isMe = currentStaff && s.id === currentStaff.id;
            const canEdit = currentStaff && (currentStaff.role === 'owner' || currentStaff.role === 'manager');
            return `
                <tr>
                    <td>
                        <div class="product-cell">
                            <div class="emoji" style="background:var(--primary);color:#fff;border-radius:50%;width:36px;height:36px;display:flex;align-items:center;justify-content:center;font-size:0.8rem;font-weight:800;">${escapeHtml(initials)}</div>
                            <div class="info">
                                <div class="name">${escapeHtml(s.full_name)} ${isMe ? '<span style="color:var(--text-muted);font-size:0.7rem;">(you)</span>' : ''}</div>
                            </div>
                        </div>
                    </td>
                    <td>${escapeHtml(s.email)}</td>
                    <td><span class="role-badge ${escapeHtml(s.role)}">${escapeHtml(s.role)}</span></td>
                    <td>
                        <span class="staff-status ${s.is_active ? 'active' : 'inactive'}">
                            <span class="dot"></span> ${s.is_active ? 'Active' : 'Deactivated'}
                        </span>
                    </td>
                    <td style="font-size:0.75rem;color:var(--text-muted);">${joined}</td>
                    <td style="text-align:right;">
                        <div class="actions-cell" style="justify-content:flex-end;">
                            ${canEdit && !isMe ? `
                                <button class="btn btn-primary btn-sm edit-staff" data-id="${s.id}" title="Edit role"><i class="fa-regular fa-pen-to-square"></i></button>
                                ${s.is_active
                                    ? `<button class="btn btn-danger btn-sm deactivate-staff" data-id="${s.id}" title="Deactivate"><i class="fa-solid fa-user-slash"></i></button>`
                                    : `<button class="btn btn-success btn-sm reactivate-staff" data-id="${s.id}" title="Reactivate"><i class="fa-solid fa-user-check"></i></button>`}
                            ` : '<span style="font-size:0.7rem;color:var(--text-muted);">—</span>'}
                        </div>
                    </td>
                </tr>
            `;
        }).join('');

        document.querySelectorAll('.edit-staff').forEach(btn => btn.addEventListener('click', () => openStaffModal(btn.dataset.id)));
        document.querySelectorAll('.deactivate-staff').forEach(btn => btn.addEventListener('click', () => deactivateStaff(btn.dataset.id)));
        document.querySelectorAll('.reactivate-staff').forEach(btn => btn.addEventListener('click', () => reactivateStaff(btn.dataset.id)));
    }

    // [FIX] Generate a friendly, high-entropy password. Excludes
    // visually ambiguous characters (I/l/1, O/0) so it can be read
    // aloud over WhatsApp without confusion.
    function generateRandomPassword(length) {
        length = length || 12;
        const chars = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789!@#$%';
        let out = '';
        if (window.crypto && window.crypto.getRandomValues) {
            const buf = new Uint32Array(length);
            window.crypto.getRandomValues(buf);
            for (let i = 0; i < length; i++) out += chars[buf[i] % chars.length];
        } else {
            for (let i = 0; i < length; i++) out += chars[Math.floor(Math.random() * chars.length)];
        }
        return out;
    }

    // [FIX] openStaffModal now hides the password field when editing
    // an existing member (only creates need a preset password), and
    // shows it when creating.
    function openStaffModal(staffId = null) {
        const modal = document.getElementById('staff-modal');
        const form = document.getElementById('staff-form');
        form.reset();
        const emailInput = document.getElementById('s-email');
        const saveBtn = document.getElementById('staff-modal-save');

        if (staffId) {
            const s = staffList.find(x => x.id === staffId);
            if (!s) return;
            document.getElementById('staff-modal-title').textContent = 'Edit Staff Member';
            document.getElementById('staff-id').value = s.id;
            document.getElementById('s-fullname').value = s.full_name || '';
            emailInput.value = s.email || '';
            emailInput.disabled = true;
            document.getElementById('s-role').value = s.role;
            // Hide password field — email and password can't be changed here.
            staffPasswordGroup.style.display = 'none';
            staffPasswordInput.required = false;
            staffPasswordInput.value = '';
            saveBtn.innerHTML = '<i class="fa-solid fa-check"></i> Update';
        } else {
            document.getElementById('staff-modal-title').textContent = 'Create Staff Member';
            document.getElementById('staff-id').value = '';
            emailInput.disabled = false;
            // Show password field — required for creation.
            staffPasswordGroup.style.display = 'block';
            staffPasswordInput.required = true;
            saveBtn.innerHTML = '<i class="fa-solid fa-user-plus"></i> Create Staff';
        }
        modal.classList.add('open');
    }
    function closeStaffModal() {
        document.getElementById('staff-modal').classList.remove('open');
        document.getElementById('staff-form').reset();
        document.getElementById('s-email').disabled = false;
        staffPasswordGroup.style.display = 'block';
        staffPasswordInput.required = true;
    }

    // [FIX] saveStaff now sends the password when creating. The PATCH
    // path for editing remains unchanged.
    async function saveStaff(e) {
        e.preventDefault();
        const id = document.getElementById('staff-id').value;
        const fullName = document.getElementById('s-fullname').value.trim();
        const email = document.getElementById('s-email').value.trim();
        const role = document.getElementById('s-role').value;
        const password = staffPasswordInput.value;
        const saveBtn = document.getElementById('staff-modal-save');
        const originalHtml = saveBtn.innerHTML;

        if (!id) {
            if (!password || password.length < 6) {
                showToast('Password must be at least 6 characters.', 'fa-solid fa-exclamation-circle');
                staffPasswordInput.focus();
                return;
            }
        }

        saveBtn.disabled = true;
        saveBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Saving...';

        try {
            if (id) {
                // Update existing staff (no password change here)
                const data = { full_name: fullName, role };
                const response = await apiFetch(`${API.staff}/${id}`, { method: 'PATCH', body: JSON.stringify(data) });
                if (!response.ok) {
                    const err = await response.json().catch(() => ({}));
                    throw new Error(err.detail || 'Update failed');
                }
                showToast('Staff member updated!');
            } else {
                // Create new staff with preset password
                const data = { email, full_name: fullName, role, password };
                const response = await apiFetch(API.staff, { method: 'POST', body: JSON.stringify(data) });
                if (!response.ok) {
                    const err = await response.json().catch(() => ({}));
                    throw new Error(err.detail || 'Create failed');
                }
                showToast('✅ Staff member created. Share the password securely.', 'fa-solid fa-check-circle');
            }
            closeStaffModal();
            await loadStaff();
        } catch (err) {
            showToast('Error: ' + err.message, 'fa-solid fa-exclamation-circle');
        } finally {
            saveBtn.disabled = false;
            saveBtn.innerHTML = originalHtml;
        }
    }

    async function deactivateStaff(id) {
        const s = staffList.find(x => x.id === id);
        if (!s) return;
        if (!confirm(`Deactivate ${s.full_name || s.email}? They will not be able to sign in.`)) return;
        try {
            const response = await apiFetch(`${API.staff}/${id}`, { method: 'DELETE' });
            if (!response.ok) {
                const err = await response.json().catch(() => ({}));
                throw new Error(err.detail || 'Deactivation failed');
            }
            showToast('Staff member deactivated.');
            await loadStaff();
        } catch (err) {
            showToast('Error: ' + err.message, 'fa-solid fa-exclamation-circle');
        }
    }

    async function reactivateStaff(id) {
        try {
            const response = await apiFetch(`${API.staff}/${id}/reactivate`, { method: 'POST' });
            if (!response.ok) {
                const err = await response.json().catch(() => ({}));
                throw new Error(err.detail || 'Reactivation failed');
            }
            showToast('Staff member reactivated.');
            await loadStaff();
        } catch (err) {
            showToast('Error: ' + err.message, 'fa-solid fa-exclamation-circle');
        }
    }

    // ═══════════════════════════════════════════════════════
    // AUDIT LOG
    // ═══════════════════════════════════════════════════════
    async function loadAuditLog(resetOffset = false) {
        if (resetOffset) auditOffset = 0;
        const params = new URLSearchParams({
            limit: auditLimit,
            offset: auditOffset,
        });
        const resourceType = auditResourceFilter.value;
        if (resourceType) params.append('resource_type', resourceType);

        try {
            const data = await fetchJSON(`${API.auditLog}?${params}`);
            const entries = data.entries || [];
            renderAuditLog(entries);
            document.getElementById('audit-page-info').textContent =
                `Page ${Math.floor(auditOffset / auditLimit) + 1} · ${data.count || 0} total`;
            document.getElementById('audit-prev').disabled = auditOffset === 0;
            document.getElementById('audit-next').disabled = (auditOffset + auditLimit) >= (data.count || 0);
        } catch (err) {
            console.error('Error loading audit log:', err);
            auditBody.innerHTML = `<tr><td colspan="5" class="empty-state">Failed to load audit log: ${escapeHtml(err.message)}</td></tr>`;
        }
    }

    function renderAuditLog(entries) {
        if (!entries || entries.length === 0) {
            auditBody.innerHTML = `<tr><td colspan="5" class="empty-state"><i class="fa-regular fa-clipboard"></i> No activity recorded yet</td></tr>`;
            return;
        }
        auditBody.innerHTML = entries.map(e => {
            const when = e.created_at ? new Date(e.created_at).toLocaleString() : '-';
            const who = e.staff_email || e.staff_id || 'System';
            const action = (e.action || 'unknown').toLowerCase();
            const actionClass = ['create','update','delete','toggle','duplicate','reorder','status_change','confirm_offline','invite','deactivate','reactivate']
                .includes(action) ? action : 'update';
            const payload = e.payload ? JSON.stringify(e.payload) : '';
            const safePayload = escapeHtml(payload);
            const payloadShort = safePayload.length > 80 ? safePayload.slice(0, 80) + '…' : safePayload;
            return `
                <tr>
                    <td style="font-size:0.75rem;color:var(--text-muted);white-space:nowrap;">${when}</td>
                    <td style="font-size:0.78rem;">${escapeHtml(who)}</td>
                    <td><span class="audit-action ${actionClass}">${escapeHtml(e.action || 'unknown')}</span></td>
                    <td>
                        <strong>${escapeHtml(e.resource_type) || '—'}</strong>
                        ${e.resource_id ? `<br><code style="font-size:0.65rem;color:var(--text-muted);">${escapeHtml(e.resource_id)}</code>` : ''}
                    </td>
                    <td>${payload ? `<span class="audit-payload" title="${safePayload}">${payloadShort}</span>` : '—'}</td>
                </tr>
            `;
        }).join('');
    }

    // ═══════════════════════════════════════════════════════
    // TAB SWITCHING
    // ═══════════════════════════════════════════════════════
    function switchTab(tab) {
        currentTab = tab;
        tabs.forEach(btn => btn.classList.toggle('active', btn.dataset.tab === tab));
        Object.keys(panels).forEach(key => panels[key] && panels[key].classList.toggle('active', key === tab));
        if (tab === 'inventory') renderInventory();
        if (tab === 'orders') renderOrders();
        if (tab === 'categories') renderCategories();
        if (tab === 'banners') loadBanners();
        if (tab === 'topproducts') {
            if (topProducts.length === 0) loadTopProducts(true);
            else renderTopProducts();
        }
        if (tab === 'areas') {
            setTimeout(() => {
                initAreaMap();
                loadDeliveryAreas();
            }, 100);
        }
        if (tab === 'deliveryrules') loadAllRules();
        if (tab === 'staff') loadStaff();
        if (tab === 'audit') loadAuditLog(true);
    }

    let searchDebounceTimer = null;
    productSearch.addEventListener('input', () => {
        clearTimeout(searchDebounceTimer);
        searchDebounceTimer = setTimeout(() => renderProducts(), 300);
    });
    orderSearch.addEventListener('input', () => {
        clearTimeout(searchDebounceTimer);
        searchDebounceTimer = setTimeout(() => renderOrders(), 300);
    });
    orderDateFilter.addEventListener('change', () => renderOrders());
    staffSearch.addEventListener('input', () => {
        clearTimeout(searchDebounceTimer);
        searchDebounceTimer = setTimeout(() => renderStaff(), 300);
    });

    // ═══════════════════════════════════════════════════════
    // EVENT LISTENERS
    // ═══════════════════════════════════════════════════════
    tabs.forEach(btn => btn.addEventListener('click', () => switchTab(btn.dataset.tab)));

    document.getElementById('add-product-btn').addEventListener('click', () => openProductModal(null));
    document.getElementById('product-modal-close').addEventListener('click', closeProductModal);
    document.getElementById('product-modal-cancel').addEventListener('click', closeProductModal);
    productForm.addEventListener('submit', saveProduct);
    productModal.addEventListener('click', (e) => { if (e.target === productModal) closeProductModal(); });

    document.getElementById('add-category-btn').addEventListener('click', () => openCategoryModal(null));
    document.getElementById('category-modal-close').addEventListener('click', closeCategoryModal);
    document.getElementById('category-modal-cancel').addEventListener('click', closeCategoryModal);
    categoryForm.addEventListener('submit', saveCategory);
    categoryModal.addEventListener('click', (e) => { if (e.target === categoryModal) closeCategoryModal(); });

    document.getElementById('order-modal-close').addEventListener('click', closeOrderModal);
    orderModal.addEventListener('click', (e) => { if (e.target === orderModal) closeOrderModal(); });

    document.getElementById('cancel-modal-close').addEventListener('click', closeCancelModal);
    document.getElementById('cancel-modal-cancel').addEventListener('click', closeCancelModal);
    cancelForm.addEventListener('submit', submitCancel);
    cancelModal.addEventListener('click', (e) => { if (e.target === cancelModal) closeCancelModal(); });

    refreshBtn.addEventListener('click', async function() {
        const originalHtml = this.innerHTML;
        this.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Refreshing...';
        this.disabled = true;
        try {
            await loadAllData(true);
            if (currentTab === 'areas') await loadDeliveryAreas();
            if (currentTab === 'deliveryrules') loadAllRules();
            if (currentTab === 'staff') await loadStaff();
            if (currentTab === 'audit') await loadAuditLog(true);
            showToast('✅ All data refreshed!');
        } catch (err) {
            showToast('❌ Error refreshing data', 'fa-solid fa-exclamation-circle');
        } finally {
            this.innerHTML = originalHtml;
            this.disabled = false;
        }
    });

    document.querySelectorAll('#inventory-view-toggle button').forEach(btn => {
        btn.addEventListener('click', function() {
            document.querySelectorAll('#inventory-view-toggle button').forEach(b => b.classList.remove('active'));
            this.classList.add('active');
            renderInventory();
        });
    });

    document.querySelectorAll('#banner-view-toggle button').forEach(btn => {
        btn.addEventListener('click', function() {
            document.querySelectorAll('#banner-view-toggle button').forEach(b => b.classList.remove('active'));
            this.classList.add('active');
            currentView = this.dataset.view;
            currentPage = 1;
            loadBanners();
        });
    });

    document.getElementById('add-banner-btn').addEventListener('click', () => openBannerModal(null));
    document.getElementById('banner-modal-close').addEventListener('click', closeBannerModal);
    document.getElementById('banner-modal-cancel').addEventListener('click', closeBannerModal);
    bannerForm.addEventListener('submit', saveBanner);
    bannerModal.addEventListener('click', (e) => { if (e.target === bannerModal) closeBannerModal(); });
    document.getElementById('banner-preview-close').addEventListener('click', () => bannerPreviewModal.classList.remove('open'));
    bannerPreviewModal.addEventListener('click', (e) => {
        if (e.target === bannerPreviewModal) bannerPreviewModal.classList.remove('open');
    });
    document.getElementById('b-cta-type').addEventListener('change', function() {
        document.getElementById('b-product-group').style.display = this.value === 'product' ? 'block' : 'none';
    });
    document.getElementById('b-image-upload').addEventListener('change', function(e) {
        const file = e.target.files[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = (event) => { document.getElementById('b-image').value = event.target.result; };
        reader.readAsDataURL(file);
    });
    document.getElementById('prev-page').addEventListener('click', () => { if (currentPage > 1) { currentPage--; loadBanners(); } });
    document.getElementById('next-page').addEventListener('click', () => { if (currentPage < totalPages) { currentPage++; loadBanners(); } });

    document.getElementById('add-area-btn').addEventListener('click', () => openAreaModal(null));
    document.getElementById('refresh-areas-btn').addEventListener('click', () => { loadDeliveryAreas(); showToast('Areas refreshed!'); });
    document.getElementById('area-modal-close').addEventListener('click', closeAreaModal);
    document.getElementById('area-modal-cancel').addEventListener('click', closeAreaModal);
    areaForm.addEventListener('submit', saveArea);
    areaModal.addEventListener('click', (e) => { if (e.target === areaModal) closeAreaModal(); });

    locationSearch.addEventListener('input', function() {
        clearTimeout(autocompleteTimeout);
        const query = this.value.trim();
        if (query.length < 2) { autocompleteDropdown.classList.remove('show'); return; }
        autocompleteTimeout = setTimeout(() => searchLocations(query), 300);
    });
    locationSearch.addEventListener('focus', function() {
        if (this.value.trim().length >= 2) autocompleteDropdown.classList.add('show');
    });
    locationSearch.addEventListener('blur', function() {
        setTimeout(() => autocompleteDropdown.classList.remove('show'), 200);
    });

    document.querySelectorAll('#rules-sub-tabs button').forEach(btn => {
        btn.addEventListener('click', () => switchSubTab(btn.dataset.subtab));
    });
    document.getElementById('refresh-rules').addEventListener('click', loadAllRules);
    document.getElementById('add-value-rule').addEventListener('click', () => openValueRuleModal(null));
    document.getElementById('add-peak-setting').addEventListener('click', () => openPeakSettingModal(null));
    document.getElementById('add-loyalty-setting').addEventListener('click', () => openLoyaltySettingModal(null));
    document.getElementById('add-item-surcharge-rule').addEventListener('click', () => openItemSurchargeModal(null));

    document.getElementById('value-rule-close').addEventListener('click', closeValueRuleModal);
    document.getElementById('value-rule-cancel').addEventListener('click', closeValueRuleModal);
    document.getElementById('value-rule-form').addEventListener('submit', saveValueRule);

    document.getElementById('peak-setting-close').addEventListener('click', closePeakSettingModal);
    document.getElementById('peak-setting-cancel').addEventListener('click', closePeakSettingModal);
    document.getElementById('peak-setting-form').addEventListener('submit', savePeakSetting);

    document.getElementById('loyalty-setting-close').addEventListener('click', closeLoyaltySettingModal);
    document.getElementById('loyalty-setting-cancel').addEventListener('click', closeLoyaltySettingModal);
    document.getElementById('loyalty-setting-form').addEventListener('submit', saveLoyaltySetting);

    document.getElementById('item-surcharge-close').addEventListener('click', closeItemSurchargeModal);
    document.getElementById('item-surcharge-cancel').addEventListener('click', closeItemSurchargeModal);
    document.getElementById('item-surcharge-form').addEventListener('submit', saveItemSurchargeRule);

    // Staff modal
    document.getElementById('add-staff-btn').addEventListener('click', () => openStaffModal(null));
    document.getElementById('staff-modal-close').addEventListener('click', closeStaffModal);
    document.getElementById('staff-modal-cancel').addEventListener('click', closeStaffModal);
    document.getElementById('staff-form').addEventListener('submit', saveStaff);
    document.getElementById('staff-modal').addEventListener('click', (e) => {
        if (e.target === document.getElementById('staff-modal')) closeStaffModal();
    });
    // [FIX] "Generate" button for initial password
    document.getElementById('s-generate-password').addEventListener('click', function() {
        staffPasswordInput.value = generateRandomPassword(12);
        // Select the text so the admin can copy it immediately
        staffPasswordInput.focus();
        staffPasswordInput.select();
    });

    document.getElementById('refresh-audit-btn').addEventListener('click', () => loadAuditLog(true));
    auditResourceFilter.addEventListener('change', () => loadAuditLog(true));
    document.getElementById('audit-prev').addEventListener('click', () => {
        if (auditOffset > 0) { auditOffset = Math.max(0, auditOffset - auditLimit); loadAuditLog(); }
    });
    document.getElementById('audit-next').addEventListener('click', () => {
        auditOffset += auditLimit;
        loadAuditLog();
    });

    orderFilterBtn.addEventListener('click', renderOrders);
    orderClearFilter.addEventListener('click', () => { orderDateFilter.value = ''; renderOrders(); });

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            if (productModal.classList.contains('open')) closeProductModal();
            if (categoryModal.classList.contains('open')) closeCategoryModal();
            if (orderModal.classList.contains('open')) closeOrderModal();
            if (bannerModal.classList.contains('open')) closeBannerModal();
            if (bannerPreviewModal.classList.contains('open')) bannerPreviewModal.classList.remove('open');
            if (areaModal.classList.contains('open')) closeAreaModal();
            if (cancelModal.classList.contains('open')) closeCancelModal();
            if (document.getElementById('value-rule-modal').classList.contains('open')) closeValueRuleModal();
            if (document.getElementById('peak-setting-modal').classList.contains('open')) closePeakSettingModal();
            if (document.getElementById('loyalty-setting-modal').classList.contains('open')) closeLoyaltySettingModal();
            if (document.getElementById('item-surcharge-modal').classList.contains('open')) closeItemSurchargeModal();
            if (document.getElementById('staff-modal').classList.contains('open')) closeStaffModal();
            if (autocompleteDropdown.classList.contains('show')) autocompleteDropdown.classList.remove('show');
        }
    });

    // ═══════════════════════════════════════════════════════
    // BOOT: Initialize Auth
    // ═══════════════════════════════════════════════════════
    async function initAuth() {
        try {
            let urlType = null;
            try {
                const hash = window.location.hash || '';
                const search = window.location.search || '';
                const hashParams = new URLSearchParams(hash.replace(/^#/, ''));
                const queryParams = new URLSearchParams(search);
                urlType = hashParams.get('type') || queryParams.get('type');
            } catch (_) {}

            const { data: { session } } = await supabaseClient.auth.getSession();

            // Invite/recovery handling: still supported for backwards
            // compatibility, though new staff no longer flow through
            // the invite email path.
            if (urlType === 'invite' || urlType === 'recovery') {
                if (session && session.user) {
                    showSetPasswordCard(session.user.email, urlType);
                } else {
                    window.history.replaceState({}, document.title, window.location.pathname);
                    showLoginScreen('That link is invalid or already used. Ask an admin to help you reset your password.');
                }
                return;
            }

            if (!session) {
                showLoginScreen();
                return;
            }

            const meRes = await apiFetch('/api/auth/me');
            if (meRes.status === 401 || meRes.status === 403) {
                await supabaseClient.auth.signOut();
                showLoginScreen();
                return;
            }
            if (!meRes.ok) {
                showLoginScreen('Server error. Please try again.');
                return;
            }
            const me = await meRes.json();
            onAuthenticated(me);
        } catch (e) {
            console.error('Auth init failed:', e);
            showLoginScreen('Network error. Please try again.');
        }
    }

    supabaseClient.auth.onAuthStateChange((event, session) => {
        if (event === 'SIGNED_OUT' && currentStaff) {
            showLoginScreen('You have been signed out.');
        }
    });

    window.addEventListener('storage', (e) => {
        if (e.key && e.key.startsWith('hotportion-admin-auth') && !e.newValue) {
            if (currentStaff) showLoginScreen('Signed out in another tab.');
        }
    });

    window.addEventListener('beforeunload', () => {
        try { stopPolling(); } catch (e) {}
    });

    (async () => {
        let attempts = 0;
        while (attempts < 20) {
            const rawHash = (window.location.hash || '').replace(/^#/, '');
            if (!rawHash) break;
            const params = new URLSearchParams(rawHash);
            if (!params.get('access_token')) break;
            const { data: { session } } = await supabaseClient.auth.getSession();
            if (session) break;
            await new Promise(r => setTimeout(r, 50));
            attempts++;
        }
        initAuth();
    })();

    console.log('📦 Hot Portion Admin loaded (RBAC enabled)');
    console.log('🔐 Auth gate active — sign in to access admin');
    console.log('👥 Staff management & audit log available to owner/manager');
    console.log('💵 Offline payment support active');
    console.log('🔑 Staff creation now uses preset passwords (no invite email)');
    console.log('🛡️ HTML escaping active on all user-controlled interpolation sites');

})();
