/**
 * Sidebar Module — Notion AI Studio
 */

window.NotionAI = window.NotionAI || {};
window.NotionAI.UI = window.NotionAI.UI || {};

window.NotionAI.UI.Sidebar = {
    toggle(show) {
        const sidebar = document.getElementById('sidebar');
        const backdrop = document.getElementById('mobileBackdrop');

        if (show) {
            sidebar.classList.add('open');
            backdrop.classList.remove('hidden');
        } else {
            sidebar.classList.remove('open');
            backdrop.classList.add('hidden');
        }
    },

    open() {
        this.toggle(true);
    },

    close() {
        this.toggle(false);
    }
};
