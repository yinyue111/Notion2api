/**
 * Validation Module
 * Handles data validation and sanitization
 */

// Initialize namespace
window.NotionAI = window.NotionAI || {};
window.NotionAI.Utils = window.NotionAI.Utils || {};

window.NotionAI.Utils.Validation = {
    /**
     * Sanitizes chat data to ensure valid structure
     * @param {Array} chats - Raw chats array from storage
     * @returns {Array} Sanitized chats array
     */
    sanitizeChats(chats) {
        if (!Array.isArray(chats)) return [];

        return chats
            .map(chat => {
                const messages = Array.isArray(chat?.messages)
                    ? chat.messages
                        .map(msg => {
                            if (!msg || typeof msg !== 'object') return null;
                            if (msg.role !== 'user' && msg.role !== 'assistant') return null;

                            const content = typeof msg.content === 'string' ? msg.content : '';
                            if (msg.role === 'user') {
                                if (!content.trim()) return null;
                                return { role: 'user', content };
                            }

                            const thinking = typeof msg.thinking === 'string' ? msg.thinking : '';
                            const search = this.normalizeSearchPayload(msg.search);
                            const modelDisplayName = typeof msg.modelDisplayName === 'string' ? msg.modelDisplayName : null;

                            return {
                                role: 'assistant',
                                content,
                                thinking,
                                search,
                                modelDisplayName
                            };
                        })
                        .filter(Boolean)
                    : [];

                return {
                    ...chat,
                    messages
                };
            })
            .filter(chat => chat && typeof chat.id !== 'undefined');
    },

    /**
     * Normalizes search payload to consistent format
     * @param {Object} payload - Raw search payload
     * @returns {Object} Normalized search data with queries and sources arrays
     */
    normalizeSearchPayload(payload) {
        const normalized = { queries: [], sources: [] };
        if (!payload || typeof payload !== 'object') return normalized;

        // Normalize queries
        if (Array.isArray(payload.queries)) {
            payload.queries.forEach(q => {
                if (typeof q === 'string' && q.trim()) {
                    normalized.queries.push(q.trim());
                }
            });
        }

        // Normalize sources (supports multiple field names)
        const sourceCandidates = [];
        if (Array.isArray(payload.sources)) sourceCandidates.push(...payload.sources);
        if (Array.isArray(payload.citations)) sourceCandidates.push(...payload.citations);

        sourceCandidates.forEach(src => {
            // Handle string sources
            if (typeof src === 'string' && src.trim()) {
                normalized.sources.push({
                    title: src.trim(),
                    url: src.trim(),
                    snippet: ''
                });
                return;
            }

            // Handle object sources
            if (!src || typeof src !== 'object') return;

            const title = String(src.title || src.name || src.sourceTitle || src.url || src.href || '').trim();
            const url = String(src.url || src.href || src.link || '').trim();
            const snippet = String(src.snippet || src.summary || src.description || '').trim();

            if (!title && !url) return;
            normalized.sources.push({ title: title || url, url, snippet });
        });

        return normalized;
    }
};
