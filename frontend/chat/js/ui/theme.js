/**
 * Theme Module — Notion AI Studio
 */

window.NotionAI = window.NotionAI || {};
window.NotionAI.UI = window.NotionAI.UI || {};

window.NotionAI.UI.Theme = {
    toggle() {
        const current = window.NotionAI.Core.State.get('theme');
        const next = current === 'dark' ? 'light' : 'dark';
        window.NotionAI.Core.State.set('theme', next);
        localStorage.setItem('theme', next);
        this.apply(next);
    },

    apply(theme) {
        document.documentElement.setAttribute('data-theme', theme);

        const sunIcon = document.getElementById('sunIcon');
        const moonIcon = document.getElementById('moonIcon');
        const themeLabel = document.getElementById('themeLabel');

        if (theme === 'dark') {
            // Also set class for any legacy checks
            document.documentElement.classList.add('dark');
            if (sunIcon) sunIcon.classList.remove('hidden');
            if (moonIcon) moonIcon.classList.add('hidden');
            if (themeLabel) themeLabel.textContent = 'Light mode';
        } else {
            document.documentElement.classList.remove('dark');
            if (sunIcon) sunIcon.classList.add('hidden');
            if (moonIcon) moonIcon.classList.remove('hidden');
            if (themeLabel) themeLabel.textContent = 'Dark mode';
        }
    },

    init() {
        const theme = window.NotionAI.Core.State.get('theme');
        this.apply(theme);
    }
};
