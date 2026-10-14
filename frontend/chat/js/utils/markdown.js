/**
 * Markdown Module
 * Handles safe markdown rendering using Marked.js and DOMPurify
 */

// Initialize namespace
window.NotionAI = window.NotionAI || {};
window.NotionAI.Utils = window.NotionAI.Utils || {};

window.NotionAI.Utils.Markdown = {
    /**
     * Cleans up footnote syntax that marked.js doesn't support
     */
    _cleanFootnotes(md) {
        if (!md) return md;
        // Remove inline footnote refs like [^1] [^2]
        let cleaned = md.replace(/\[\^(\d+)\]/g, '');
        // Remove footnote definition blocks at the end: [^1]: http://...
        cleaned = cleaned.replace(/^\[\^\d+\]:\s*.*$/gm, '');
        // Clean up trailing blank lines left behind
        cleaned = cleaned.replace(/\n{3,}/g, '\n\n');
        return cleaned.trim();
    },

    /**
     * Renders markdown to safe HTML
     * @param {string} markdown - Raw markdown string
     * @returns {string} Sanitized HTML
     */
    renderToSafeHtml(markdown) {
        const cleaned = this._cleanFootnotes(markdown || '');
        const renderedHtml = marked.parse(cleaned);
        return DOMPurify.sanitize(renderedHtml, {
            USE_PROFILES: { html: true },
            ADD_ATTR: ['target', 'rel']
        });
    },

    /**
     * Sets safe markdown content to a container element
     * @param {HTMLElement} container - Target DOM element
     * @param {string} markdown - Raw markdown string
     */
    setSafeMarkdown(container, markdown) {
        container.innerHTML = this.renderToSafeHtml(markdown);
    }
};
