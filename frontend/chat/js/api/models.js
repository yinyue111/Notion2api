/**
 * Models Module
 * Handles model selection and management
 */

// Initialize namespace
window.NotionAI = window.NotionAI || {};
window.NotionAI.API = window.NotionAI.API || {};

window.NotionAI.API.Models = {
    // Current selected model
    _currentModel: window.NotionAI.Core.Constants.DEFAULT_MODEL,
    _currentModelLabel: null,

    /**
     * Gets the current selected model ID
     * @returns {string} Model ID
     */
    getCurrentModel() {
        return this._currentModel;
    },

    /**
     * Gets the current selected model label
     * @returns {string} Model display label
     */
    getCurrentModelLabel() {
        if (!this._currentModelLabel) {
            this._currentModelLabel = this.getModelDisplayName(this._currentModel);
        }
        return this._currentModelLabel;
    },

    /**
     * Sets the current selected model
     * @param {string} modelId - Model ID
     * @param {string} label - Model display label
     */
    setCurrentModel(modelId, label) {
        this._currentModel = modelId;
        this._currentModelLabel = label;
    },

    /**
     * Gets display name for a model ID
     * @param {string} modelId - Model ID
     * @returns {string} Display name
     */
    getModelDisplayName(modelId) {
        const names = window.NotionAI.Core.State.get('modelDisplayNames');
        return names[modelId] || modelId;
    },

    /**
     * Loads model display names from constants
     */
    loadModels() {
        const modelDisplayNames = {
            ...window.NotionAI.Core.Constants.MODEL_DISPLAY_NAMES
        };
        window.NotionAI.Core.State.set('modelDisplayNames', modelDisplayNames);
    },

    /**
     * Gets all available models
     * @returns {Array} Array of model objects
     */
    getAvailableModels() {
        return window.NotionAI.Core.Constants.MODELS;
    }
};
