import { state } from './state.js';
import { dom } from './dom.js';
import { formatPrice, showToast, highlightText } from './utils.js';
import { amazonPlacesFetch, checkDeliveryCoverage } from './api.js';
import { updateCartUI, persistState } from './cart.js';

// ─── GEOCODING HELPERS (Amazon Location) ───
export async function geocodeAddress(address) {
    const data = await amazonPlacesFetch('geocode', {
        QueryText: address,
        MaxResults: 1,
        Filter: { IncludeCountries: ['NGA'] },
    });
    const items = data.ResultItems || data.Results || [];
    if (items.length === 0) throw new Error('Address not found');
    const first = items[0];
    const pos = first.Position || first.Place?.Geometry?.Point;
    if (!pos) throw new Error('Address has no coordinates');
    const [lng, lat] = pos;
    return {
        lat,
        lng,
        display_name: first.Address?.Label || first.Title || address,
    };
}

export async function reverseGeocode(lat, lng) {
    const data = await amazonPlacesFetch('reverse-geocode', {
        QueryPosition: [lng, lat],
        MaxResults: 1,
    });
    const items = data.ResultItems || data.Results || [];
    if (items.length === 0) throw new Error('Location not found');
    const first = items[0];
    return first.Address?.Label || first.Title || '';
}

export function getCurrentPosition() {
    return new Promise((resolve, reject) => {
        if (!navigator.geolocation) {
            reject(new Error('Geolocation not supported by your browser'));
            return;
        }
        navigator.geolocation.getCurrentPosition(
            (position) => {
                resolve({
                    lat: position.coords.latitude,
                    lng: position.coords.longitude
                });
            },
            (error) => {
                let message = 'Unable to get location';
                switch (error.code) {
                    case error.PERMISSION_DENIED:
                        message = 'Location permission denied. Please enable GPS in your browser settings.';
                        break;
                    case error.POSITION_UNAVAILABLE:
                        message = 'Location information unavailable. Please check your GPS signal.';
                        break;
                    case error.TIMEOUT:
                        message = 'Location request timed out. Please try again.';
                        break;
                }
                reject(new Error(message));
            }, {
                enableHighAccuracy: true,
                timeout: 15000,
                maximumAge: 60000
            }
        );
    });
}

// ─── Update delivery breakdown UI ───
export function updateBreakdownUI(breakdown) {
    if (!breakdown) {
        dom.deliveryBreakdown.classList.remove('show');
        return;
    }

    dom.breakdownBase.textContent = formatPrice(breakdown.base || 0);
    dom.breakdownDiscount.textContent = `-${formatPrice(Math.abs(breakdown.value_discount || 0))}`;
    dom.breakdownSurcharge.textContent = `+${formatPrice(breakdown.item_surcharge || 0)}`;
    dom.breakdownPeak.textContent = `+${formatPrice(breakdown.peak_surcharge || 0)}`;
    dom.breakdownLoyalty.textContent = `-${formatPrice(Math.abs(breakdown.loyalty_discount || 0))}`;
    dom.breakdownTotal.textContent = formatPrice(breakdown.final || 0);

    const hasAdjustments = breakdown.value_discount > 0 ||
        breakdown.item_surcharge > 0 ||
        breakdown.peak_surcharge > 0 ||
        breakdown.loyalty_discount > 0;

    if (hasAdjustments) {
        dom.deliveryBreakdown.classList.add('show');
    } else {
        dom.deliveryBreakdown.classList.remove('show');
    }
}

// ─── DELIVERY ADDRESS HANDLER ───
let deliveryDebounceTimer = null;

export async function handleDeliveryAddressChange(precomputedCoords = null) {
    const address = dom.deliveryAddress.value.trim();

    if (address.length < 5) {
        resetDeliveryStatus();
        return;
    }

    if (address === state.lastCheckedAddress && state.isDeliveryCovered && state.lastBreakdown) {
        return;
    }

    state.lastCheckedAddress = address;

    let coords = precomputedCoords;
    if (!coords) {
        try {
            const geo = await geocodeAddress(address);
            coords = { lat: geo.lat, lng: geo.lng };
            console.log('📍 Amazon geocode ok:', coords);
        } catch (e) {
            console.warn('Amazon geocode failed, backend will fall back to Nominatim:', e.message);
        }
    }

    const result = await checkDeliveryCoverage(address, coords);

    if (result.unavailable) {
        state.deliveryFee = 0;
        state.isDeliveryCovered = false;
        state.isDeliveryAvailable = false;
        state.lastBreakdown = null;

        dom.deliveryStatus.className = 'delivery-status not-covered';
        dom.deliveryStatus.innerHTML =
            '⚠️ We can\'t calculate the delivery fee right now. ' +
            '<button type="button" id="delivery-retry" ' +
            'style="background:none;border:none;color:var(--primary);font-weight:700;' +
            'text-decoration:underline;cursor:pointer;padding:0;font-size:inherit;">Retry</button>' +
            ' or choose Pickup / Dine-in.';

        const retryBtn = document.getElementById('delivery-retry');
        if (retryBtn) {
            retryBtn.addEventListener('click', function () {
                state.lastCheckedAddress = '';
                handleDeliveryAddressChange();
            });
        }

        updateDeliveryFeeUI(0);
        dom.deliveryBreakdown.classList.remove('show');
        import('./checkout.js').then(({ updateCheckoutButton }) => updateCheckoutButton()).catch(() => {});
        return;
    }

    // Pricing API answered — reset availability flag
    state.isDeliveryAvailable = true;

    if (result.covered) {
        state.deliveryFee = result.total_fee || result.fee || 0;
        state.isDeliveryCovered = true;
        state.lastBreakdown = result.breakdown || null;

        const areaMsg = result.area_name ? ` (${result.area_name})` : '';
        updateDeliveryStatus('covered', `✅ We deliver to your area! Fee: ${formatPrice(state.deliveryFee)}${areaMsg}`);
        updateDeliveryFeeUI(state.deliveryFee);

        if (state.lastBreakdown) {
            updateBreakdownUI(state.lastBreakdown);
        }
    } else {
        state.deliveryFee = 0;
        state.isDeliveryCovered = false;
        state.lastBreakdown = null;
        updateDeliveryStatus('not-covered', result.message || '❌ Sorry, we don\'t deliver to this area yet.');
        updateDeliveryFeeUI(0);
        dom.deliveryBreakdown.classList.remove('show');
    }
}

export function updateDeliveryStatus(type, message) {
    dom.deliveryStatus.className = 'delivery-status ' + type;
    dom.deliveryStatus.textContent = message;
}

export function resetDeliveryStatus() {
    dom.deliveryStatus.className = 'delivery-status';
    dom.deliveryStatus.textContent = '';
    state.isDeliveryCovered = false;
    state.isDeliveryAvailable = true;
    state.deliveryFee = 0;
    state.lastBreakdown = null;
    updateDeliveryFeeUI(0);
    dom.deliveryBreakdown.classList.remove('show');
}

export function updateDeliveryFeeUI(fee) {
    dom.deliveryFeeDisplay.innerHTML = `Delivery fee: <span>${formatPrice(fee)}</span>
        <span class="fee-breakdown-toggle" id="fee-breakdown-toggle">Show breakdown</span>`;

    const newToggle = document.getElementById('fee-breakdown-toggle');
    if (newToggle) {
        newToggle.addEventListener('click', function() {
            dom.deliveryBreakdown.classList.toggle('show');
            this.textContent = dom.deliveryBreakdown.classList.contains('show') ? 'Hide breakdown' : 'Show breakdown';
        });
    }

    updateCartUI();
}

// ─── GPS BUTTON HANDLER ───
export async function handleGPSButton() {
    if (state.deliveryMethod !== 'delivery') {
        showToast('Please select "Delivery" first.', 'fa-solid fa-exclamation-circle');
        return;
    }

    dom.gpsBtn.disabled = true;
    dom.gpsBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Locating...';

    try {
        const position = await getCurrentPosition();
        const address = await reverseGeocode(position.lat, position.lng);

        if (address) {
            dom.deliveryAddress.value = address;
            await handleDeliveryAddressChange({
                lat: position.lat,
                lng: position.lng
            });
            persistState();
            showToast('📍 Location found!', 'fa-solid fa-check-circle');
        }
    } catch (err) {
        showToast('GPS error: ' + err.message, 'fa-solid fa-exclamation-circle');
        console.warn('GPS error:', err);
    } finally {
        dom.gpsBtn.disabled = false;
        dom.gpsBtn.innerHTML = '<i class="fa-solid fa-location-crosshairs" aria-hidden="true"></i> GPS';
    }
}

// ─── ADDRESS AUTOCOMPLETE (Amazon Location) ───
export async function searchAddresses(query) {
    if (!query || query.length < 2) {
        dom.addressDropdown.classList.remove('show');
        return;
    }

    const loadingEl = dom.addressDropdown.querySelector('.address-autocomplete-loading');
    const emptyEl = dom.addressDropdown.querySelector('.address-autocomplete-empty');
    loadingEl.style.display = 'block';
    emptyEl.style.display = 'none';
    dom.addressDropdown.classList.add('show');

    try {
        const data = await amazonPlacesFetch('autocomplete', {
            QueryText: query,
            MaxResults: 6,
            Language: 'en',
            Filter: { IncludeCountries: ['NGA'] },
        });

        loadingEl.style.display = 'none';

        const items = dom.addressDropdown.querySelectorAll('.address-autocomplete-item');
        items.forEach(el => el.remove());

        const results = data.ResultItems || data.Results || [];
        if (results.length === 0) {
            emptyEl.style.display = 'block';
            return;
        }

        emptyEl.style.display = 'none';

        results.forEach(result => {
            const item = document.createElement('div');
            item.className = 'address-autocomplete-item';

            const displayName = result.Address?.Label || result.Title || result.Text || 'Unknown location';
            const subText = [
                result.Address?.Locality,
                result.Address?.Region?.Name,
                result.Address?.Country?.Name,
            ].filter(Boolean).join(', ');

            item.innerHTML = `
                <span>${highlightText(displayName, query)}</span>
                ${subText ? `<span class="sub-text">${highlightText(subText, query)}</span>` : ''}
            `;

            item.addEventListener('mousedown', function(e) {
                e.preventDefault();
                dom.deliveryAddress.value = displayName;
                dom.addressDropdown.classList.remove('show');
                dom.deliveryAddress.dispatchEvent(new Event('input', { bubbles: true }));
            });

            dom.addressDropdown.appendChild(item);
        });

    } catch (err) {
        console.warn('Autocomplete error:', err);
        loadingEl.style.display = 'none';
        emptyEl.style.display = 'block';
        emptyEl.textContent = 'Error searching locations';
    }
}

export function setDeliveryDebounceTimer(fn, delay) {
    clearTimeout(deliveryDebounceTimer);
    deliveryDebounceTimer = setTimeout(fn, delay);
}
