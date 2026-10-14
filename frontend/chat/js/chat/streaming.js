/**
 * Streaming Module
 * Handles SSE streaming responses from backend
 */

// Initialize namespace
window.NotionAI = window.NotionAI || {};
window.NotionAI.Chat = window.NotionAI.Chat || {};

window.NotionAI.Chat.Streaming = {
    /**
     * Streams chat completion response
     * @param {Object} chat - Current chat object
     * @param {string} model - Model ID to use
     * @param {Object} aiWrapper - AI message wrapper element
     * @returns {Promise<Object>} Result with full reply, thinking text, and search data
     */
    async streamResponse(chat, model, aiWrapper) {
        const searchState = { queries: [], sources: [] };
        let thinkingText = '';
        let fullAiReply = '';

        // Prepare messages
        const requestMessages = chat.messages
            .filter(msg => (
                msg &&
                typeof msg === 'object' &&
                (msg.role === 'user' || msg.role === 'assistant')
            ))
            .map(msg => ({
                role: msg.role,
                content: String(msg.content || ''),
                thinking: String(msg.thinking || '')
            }));

        // Create request
        STATE.controller = new AbortController();

        try {
            const response = await fetch(
                `${window.NotionAI.Core.State.get('baseUrl')}/v1/chat/completions`,
                {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Authorization': `Bearer ${window.NotionAI.Core.State.get('apiKey')}`,
                        'X-Client-Type': window.NotionAI.Core.Constants.CLIENT_TYPE
                    },
                    body: JSON.stringify({
                        model: model,
                        messages: requestMessages,
                        conversation_id: chat.conversationId || null,
                        stream: true
                    }),
                    signal: STATE.controller.signal
                }
            );

            // Check memory status
            const isMemoryDegraded = window.NotionAI.API.Client.checkMemoryStatus(response);
            if (isMemoryDegraded) {
                this.notifyMemoryDegradedOnce();
            }

            // Extract conversation ID
            const backendConversationId = window.NotionAI.API.Client.getConversationId(response);
            if (backendConversationId && chat.conversationId !== backendConversationId) {
                window.NotionAI.Chat.Storage.updateConversationId(chat.id, backendConversationId);
                chat.conversationId = backendConversationId;
            }

            if (!response.ok) {
                // Read the response body to get structured error info
                let errorInfo = { message: `HTTP Error: ${response.status}`, code: '', suggestion: '' };
                try {
                    const errorBody = await response.json();
                    if (errorBody?.error) {
                        errorInfo.message = errorBody.error.message || errorInfo.message;
                        errorInfo.code = errorBody.error.code || '';
                        errorInfo.suggestion = errorBody.error.suggestion || '';
                        errorInfo.detail = errorBody.error.detail || '';
                    } else if (errorBody?.detail) {
                        // FastAPI HTTPException format
                        errorInfo.message = errorBody.detail;
                    }
                } catch (e) {
                    // Response body is not JSON, use status code
                }

                if (response.status === 401) {
                    errorInfo.message = errorInfo.message || "API KEY doesn't match.";
                    errorInfo.suggestion = errorInfo.suggestion || "Check your API Key in Settings.";
                }

                const err = new Error(errorInfo.message);
                err.errorCode = errorInfo.code;
                err.suggestion = errorInfo.suggestion;
                err.errorDetail = errorInfo.detail;
                err.httpStatus = response.status;
                throw err;
            }

            // Process stream
            const result = await this.processStream(response, aiWrapper, searchState, thinkingText, fullAiReply);
            return result;

        } catch (err) {
            if (err.name !== 'AbortError') {
                console.error('API Error:', err);
                window.NotionAI.Chat.Renderer.showErrorCard(
                    aiWrapper,
                    err.message,
                    err.errorCode || '',
                    err.suggestion || '',
                    err.errorDetail || '',
                    err.httpStatus || 0
                );
            }
            throw err;
        }
    },

    /**
     * Processes SSE stream from response
     * @param {Response} response - Fetch response
     * @param {Object} aiWrapper - AI message wrapper
     * @param {Object} searchState - Search state object
     * @param {string} thinkingText - Thinking text accumulator
     * @param {string} fullAiReply - Reply text accumulator
     * @returns {Promise<Object>} Final result object
     */
    async processStream(response, aiWrapper, searchState, thinkingText, fullAiReply) {
        const reader = response.body.getReader();
        const decoder = new TextDecoder('utf-8');
        let sseBuffer = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            sseBuffer += decoder.decode(value, { stream: true });
            const events = sseBuffer.split('\n\n');
            sseBuffer = events.pop() || '';

            for (const eventBlock of events) {
                const lines = eventBlock.split('\n');
                for (let line of lines) {
                    line = line.trim();
                    if (!line.startsWith('data:')) continue;

                    const payload = line.slice(5).trim();
                    const result = this.consumePayload(payload, aiWrapper, searchState, thinkingText, fullAiReply);

                    if (result.thinkingText !== undefined) {
                        thinkingText = result.thinkingText;
                    }
                    if (result.fullAiReply !== undefined) {
                        fullAiReply = result.fullAiReply;
                    }
                }
            }
        }

        // Process remaining buffer
        if (sseBuffer.trim().startsWith('data:')) {
            const payload = sseBuffer.trim().slice(5).trim();
            const result = this.consumePayload(payload, aiWrapper, searchState, thinkingText, fullAiReply);
            if (result.thinkingText !== undefined) {
                thinkingText = result.thinkingText;
            }
            if (result.fullAiReply !== undefined) {
                fullAiReply = result.fullAiReply;
            }
        }

        return { fullAiReply, thinkingText, searchState };
    },

    /**
     * Consumes a single SSE payload
     * @param {string} payload - SSE data payload
     * @param {Object} aiWrapper - AI message wrapper
     * @param {Object} searchState - Search state object
     * @param {string} thinkingText - Current thinking text
     * @param {string} fullAiReply - Current AI reply
     * @returns {Object} Updated state
     */
    consumePayload(payload, aiWrapper, searchState, thinkingText, fullAiReply) {
        if (!payload || payload === '[DONE]') {
            return { thinkingText, fullAiReply };
        }

        let dataObj;
        try {
            dataObj = JSON.parse(payload);
        } catch (e) {
            return { thinkingText, fullAiReply };
        }

        // Handle search metadata
        if (dataObj?.type === 'search_metadata') {
            this.mergeSearchState(searchState, dataObj.searches || {});
            aiWrapper.searchData = searchState;
            window.NotionAI.Chat.Renderer.updateSearchPanel(aiWrapper);
            return { thinkingText, fullAiReply };
        }

        // Handle thinking chunk
        if (dataObj?.type === 'thinking_chunk') {
            const chunk = typeof dataObj.text === 'string' ? dataObj.text : '';
            if (chunk) {
                thinkingText += chunk;
                aiWrapper.thinkingText = thinkingText;
                window.NotionAI.Chat.Renderer.updateThinkingPanel(aiWrapper);
            }
            return { thinkingText, fullAiReply };
        }

        // Handle content replace
        if (dataObj?.type === 'content_replace') {
            const replacement = typeof dataObj.content === 'string' ? dataObj.content : '';
            if (replacement) {
                fullAiReply = replacement;
                window.NotionAI.Chat.Renderer.updateAIMessage(aiWrapper, fullAiReply, false);
            }
            return { thinkingText, fullAiReply };
        }

        // Handle thinking replace
        if (dataObj?.type === 'thinking_replace') {
            thinkingText = typeof dataObj.thinking === 'string' ? dataObj.thinking : '';
            aiWrapper.thinkingText = thinkingText;
            window.NotionAI.Chat.Renderer.updateThinkingPanel(aiWrapper);
            return { thinkingText, fullAiReply };
        }

        // Handle delta reasoning
        const deltaReasoning = dataObj?.choices?.[0]?.delta?.reasoning_content || '';
        if (deltaReasoning) {
            thinkingText += deltaReasoning;
            aiWrapper.thinkingText = thinkingText;
            window.NotionAI.Chat.Renderer.updateThinkingPanel(aiWrapper);
            return { thinkingText, fullAiReply };
        }

        // Handle delta content
        const deltaContent = dataObj?.choices?.[0]?.delta?.content || '';
        if (deltaContent) {
            fullAiReply += deltaContent;
            window.NotionAI.Chat.Renderer.updateAIMessage(aiWrapper, fullAiReply, false);
            return { thinkingText, fullAiReply };
        }

        return { thinkingText, fullAiReply };
    },

    /**
     * Merges search state from payload
     * @param {Object} target - Target search state
     * @param {Object} payload - Search payload
     */
    mergeSearchState(target, payload) {
        const normalized = window.NotionAI.Utils.Validation.normalizeSearchPayload(payload);

        normalized.queries.forEach(query => {
            if (!target.queries.includes(query)) {
                target.queries.push(query);
            }
        });

        normalized.sources.forEach(source => {
            const exists = target.sources.some(existing =>
                existing.title === source.title && existing.url === source.url
            );
            if (!exists) {
                target.sources.push(source);
            }
        });
    },

    /**
     * Notifies user of memory degradation (once per session)
     */
    notifyMemoryDegradedOnce() {
        if (!this._memoryNotified) {
            this._memoryNotified = true;
            const banner = document.getElementById('memoryBanner');
            if (banner) {
                banner.classList.remove('hidden');
            }
        }
    }
};
