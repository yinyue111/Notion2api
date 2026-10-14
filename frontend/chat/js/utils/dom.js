/**
 * DOM Utility Module
 * Provides common DOM manipulation helpers
 */

// Initialize namespace
window.NotionAI = window.NotionAI || {};
window.NotionAI.Utils = window.NotionAI.Utils || {};

window.NotionAI.Utils.DOM = {
    /**
     * Copies text to clipboard with visual feedback
     * @param {HTMLElement} btn - Button element for feedback
     * @param {string} text - Text to copy
     */
    copyText(btn, text) {
        navigator.clipboard.writeText(text).then(() => {
            const originalSvg = btn.innerHTML;
            btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"></polyline></svg>';
            setTimeout(() => btn.innerHTML = originalSvg, 2000);
        });
    },

    /**
     * Adds copy buttons to code blocks within a container
     * @param {HTMLElement} container - Container element to search for code blocks
     */
    addCodeBlockCopyButtons(container) {
        container.querySelectorAll('pre').forEach(pre => {
            // Avoid adding duplicate buttons
            if (pre.querySelector('.code-copy-btn')) return;

            const code = pre.querySelector('code');
            if (!code) return;

            const btn = document.createElement('button');
            btn.className = 'code-copy-btn';
            btn.textContent = 'Copy';
            btn.onclick = () => {
                this.copyText(btn, code.innerText);
                btn.textContent = 'Copied!';
                setTimeout(() => btn.textContent = 'Copy', 2000);
            };
            pre.appendChild(btn);
        });
    },

    /**
     * Scrolls chat container to bottom smoothly
     */
    scrollToBottom() {
        const chatContainer = document.getElementById('chatContainer');
        if (chatContainer) {
            chatContainer.scrollTo({
                top: chatContainer.scrollHeight,
                behavior: 'smooth'
            });
        }
    }
};
