/**
 * Input Module — Notion AI Studio
 */

window.NotionAI = window.NotionAI || {};
window.NotionAI.UI = window.NotionAI.UI || {};

window.NotionAI.UI.Input = {
    autoResize() {
        const input = document.getElementById('chatInput');
        input.style.height = '48px';
        const scrollHeight = input.scrollHeight;
        input.style.height = Math.min(scrollHeight, 160) + 'px';
    },

    handleKeydown(e, onSend) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            onSend();
        }
    },

    clear() {
        const input = document.getElementById('chatInput');
        input.value = '';
        this.autoResize();
    },

    focus() {
        const input = document.getElementById('chatInput');
        input.focus();
    },

    getValue() {
        const input = document.getElementById('chatInput');
        return input.value.trim();
    },

    enable() {
        const input = document.getElementById('chatInput');
        const sendBtn = document.getElementById('sendBtn');
        input.disabled = false;
        sendBtn.disabled = false;
    },

    disable() {
        const input = document.getElementById('chatInput');
        input.disabled = true;
        // Don't disable send btn — it becomes stop btn during generation
    }
};
