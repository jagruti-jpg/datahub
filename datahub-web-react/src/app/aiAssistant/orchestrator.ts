const AI_CHAT_ENDPOINT = (import.meta as any)?.env?.VITE_AI_CHAT_ENDPOINT || 'http://localhost:8000/api/ai/chat';

/**
 * Base origin of the ai-orchestrator, derived from the chat endpoint.
 *
 * Orchestrator routes must be addressed absolutely, not as paths relative to the page. The
 * orchestrator is a separate service, and only the Vite dev server proxies `/api/skills`
 * and friends to it — the datahub-frontend container proxies `/api/*` to GMS, where those
 * routes do not exist. A relative path therefore works on :3000 and 404s on :9002.
 */
export const AI_ORCHESTRATOR_BASE: string = AI_CHAT_ENDPOINT.replace(/\/api\/ai\/chat\/?$/, '');

export function orchestratorUrl(path: string): string {
    return `${AI_ORCHESTRATOR_BASE}${path}`;
}

export { AI_CHAT_ENDPOINT };
