/**
 * Modal Module — Notion AI Studio
 */

window.NotionAI = window.NotionAI || {};
window.NotionAI.UI = window.NotionAI.UI || {};

window.NotionAI.UI.Modal = {
    openRenameModal(chatId) {
        const chats = window.NotionAI.Core.State.get('chats');
        const chat = chats.find(c => c.id === chatId);
        if (!chat) return;

        window.NotionAI.Core.State.set('chatToRename', chatId);

        const modal = document.getElementById('renameModal');
        const input = document.getElementById('renameModalInput');

        input.value = chat.title;
        modal.classList.remove('hidden');
        setTimeout(() => input.focus(), 50);
    },

    closeRenameModal() {
        const modal = document.getElementById('renameModal');
        modal.classList.add('hidden');
        window.NotionAI.Core.State.set('chatToRename', null);
    },

    saveRename() {
        const chatId = window.NotionAI.Core.State.get('chatToRename');
        if (!chatId) return;

        const input = document.getElementById('renameModalInput');
        const newTitle = input.value.trim();

        if (newTitle) {
            window.NotionAI.Chat.Manager.renameChat(chatId, newTitle);
        }

        this.closeRenameModal();
    }
};
