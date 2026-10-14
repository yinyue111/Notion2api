/**
 * Settings Module — Notion AI Studio
 */

window.NotionAI = window.NotionAI || {};
window.NotionAI.API = window.NotionAI.API || {};

window.NotionAI.API.Settings = {
    open() {
        const baseUrl = window.NotionAI.Core.State.get('baseUrl');
        const apiKey = window.NotionAI.Core.State.get('apiKey');

        document.getElementById('baseUrlInput').value = baseUrl;
        document.getElementById('apiKeyInput').value = apiKey;

        document.getElementById('settingsModal').classList.remove('hidden');
    },

    close() {
        document.getElementById('settingsModal').classList.add('hidden');
    },

    save() {
        const baseUrl = document.getElementById('baseUrlInput').value.trim().replace(/\/$/, "");
        const apiKey = document.getElementById('apiKeyInput').value.trim();

        window.NotionAI.Core.State.set('baseUrl', baseUrl);
        window.NotionAI.Core.State.set('apiKey', apiKey);

        localStorage.setItem('claude_base_url', baseUrl);
        window.NotionAI.Core.State.persistApiKey(apiKey);

        this.close();
        window.NotionAI.API.Models.loadModels();
    }
};
