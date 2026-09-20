import { API_BASE } from './config.js';
import { state } from './state.js';
import { dom } from './dom.js';
import { showToast } from './utils.js';
import { addToCart } from './cart.js';

// ─── CHAT FUNCTIONS ───
export function addChatMsg(sender, text, isTyping = false, extraHtml = '') {
    if (!dom.chatMessages) return;
    const row = document.createElement('div');
    row.className = `chat-msg ${sender === 'user' ? 'user' : 'assistant'}`;
    if (sender === 'assistant') {
        const avatar = document.createElement('div');
        avatar.className = 'chat-msg-avatar';
        avatar.innerHTML = '<i class="fa-regular fa-comment-dots"></i>';
        row.appendChild(avatar);
    }
    const bubble = document.createElement('div');
    bubble.className = 'chat-msg-bubble';
    if (isTyping && sender === 'assistant') {
        bubble.innerHTML = '<div class="thinking-dots"><span></span><span></span><span></span></div>';
    } else {
        const parsed = marked.parse(text);
        const combined = parsed + extraHtml;
        if (typeof DOMPurify !== 'undefined') {
            bubble.innerHTML = DOMPurify.sanitize(combined, { ADD_ATTR: ['data-id'] });
        } else {
            console.warn('DOMPurify unavailable — falling back to text-only rendering');
            bubble.textContent = text;
            if (extraHtml) {
                const extraWrap = document.createElement('div');
                extraWrap.innerHTML = extraHtml;
                bubble.appendChild(extraWrap);
            }
        }
    }
    row.appendChild(bubble);
    dom.chatMessages.appendChild(row);
    dom.chatMessages.scrollTop = dom.chatMessages.scrollHeight;
    return row;
}

export async function sendChatMessage() {
    const msg = dom.chatInput.value.trim();
    if (!msg) return;
    addChatMsg('user', msg);
    dom.chatInput.value = '';
    const typingRow = addChatMsg('assistant', '', true);

    try {
        const response = await fetch(`${API_BASE}/api/ai/chat`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message: msg })
        });

        if (!response.ok) throw new Error('AI service unavailable');

        const data = await response.json();
        if (typingRow) typingRow.remove();

        let reply = data.response || "I'm sorry, I couldn't understand that.";
        let extraHtml = '';

        const productMatch = state.products.find(p => reply.toLowerCase().includes(p.name.toLowerCase()));
        if (productMatch) {
            extraHtml =
                ` <button class="chat-add-to-cart" style="background:var(--primary);color:#fff;border:none;padding:4px 12px;border-radius:50px;font-size:11px;font-weight:700;cursor:pointer;margin-top:6px;" data-id="${productMatch.id}">➕ Add to Cart</button>`;
        }

        addChatMsg('assistant', reply, false, extraHtml);

        document.querySelectorAll('.chat-add-to-cart').forEach(btn => {
            btn.addEventListener('click', function() {
                const id = this.dataset.id;
                addToCart(id, 1);
                showToast('Added to cart!', 'fa-solid fa-check-circle');
                this.style.background = '#28a745';
                this.textContent = '✓ Added';
                this.disabled = true;
            });
        });

    } catch (err) {
        if (typingRow) typingRow.remove();
        addChatMsg('assistant', '⚠️ Sorry, the AI assistant is currently offline. Please try again later.');
        console.error('Chat error:', err);
    }
}

export function initChat() {
    if (dom.chatMessages.children.length === 0 && state.isDataLoaded) {
        addChatMsg('assistant',
            `Hi! I'm your HotPortion AI. We have ${state.products.length} meals across ${state.categories.length} categories. Try asking: *'I want something filling under ₦5,000'* or *'What combos do you have?'* 🍔`
            );
    }
}

export function toggleChat() {
    dom.chatModal.classList.toggle('active');
    if (dom.chatModal.classList.contains('active')) {
        initChat();
        dom.chatInput.focus();
    }
}
