// =============================================================================
// view-utils.ts -- Shared utilities for viz view modules
//
// Consolidates duplicated helpers from detail.js, list.js, docs.js, tests.js,
// workflow.js: HTML escaping, Monaco AMD init, badge rendering, path helpers.
// =============================================================================
// ── HTML escape ─────────────────────────────────────────────────────────────
/** Escape a string for safe HTML insertion. Handles null/undefined. */
export function esc(s) {
    return String(s ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}
/** Alias for esc -- used in App module for consistency. */
export const escHtml = esc;
// ── Monaco AMD loader ───────────────────────────────────────────────────────
const MONACO_CDN = 'https://cdnjs.cloudflare.com/ajax/libs/monaco-editor/0.52.0/min/vs';
let monacoLoaded = false;
const pendingCallbacks = [];
/**
 * Ensure Monaco editor is loaded via AMD, then call the callback.
 * Multiple callers share a single require() call -- if Monaco is already
 * loaded the callback fires synchronously.
 */
export function ensureMonaco(callback) {
    if (monacoLoaded) {
        callback();
        return;
    }
    pendingCallbacks.push(callback);
    // Only trigger the require once
    if (pendingCallbacks.length === 1) {
        const req = window.require;
        if (typeof req === 'undefined')
            return;
        req.config({ paths: { vs: MONACO_CDN } });
        req(['vs/editor/editor.main'], () => {
            monacoLoaded = true;
            for (const cb of pendingCallbacks) {
                try {
                    cb();
                }
                catch (e) {
                    console.error('Monaco callback error:', e);
                }
            }
            pendingCallbacks.length = 0;
        });
    }
}
/** Check if Monaco has been loaded. */
export function isMonacoReady() {
    return monacoLoaded;
}
// ── Badge rendering ─────────────────────────────────────────────────────────
/** Render a staleness badge as an HTML string. */
export function stalenessBadge(status) {
    const label = status === 'all' ? '' : status.replace(/_/g, ' ').toLowerCase();
    return `<span class="staleness-badge ${esc(status)}">${esc(label)}</span>`;
}
/** Render a subtype badge as an HTML string. */
export function subtypeBadge(subtype) {
    return `<span class="subtype-badge ${esc(subtype)}">${esc(subtype)}</span>`;
}
/** Render a tag chip as an HTML string. */
export function tagChip(tag) {
    return `<span class="tag-chip">${esc(tag)}</span>`;
}
// ── Path helpers ────────────────────────────────────────────────────────────
/** Extract short title from a cortex node ID (part after last '::'). */
export function shortTitle(nodeId) {
    return nodeId.split('::').pop() || nodeId;
}
/** Extract the module/file portion of a node location. */
export function locationModule(location) {
    if (!location)
        return '';
    const parts = location.split(':');
    return parts[0] || location;
}
// ── Refresh SVG icon (shared by tests.js, workflow.js) ──────────────────────
export const refreshSvg = `<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 8a5.5 5.5 0 0 1 9.3-4"/><path d="M13.5 8a5.5 5.5 0 0 1-9.3 4"/><path d="M11.5 1.5v3h3"/><path d="M4.5 14.5v-3h-3"/></svg>`;
// ── Collapse All SVG icon (double-chevron, shared by tests.ts, workflow.ts) ─
export const collapseAllSvg = `<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M4 6l4 4 4-4M4 10l4 4 4-4"/></svg>`;
// ── API fetch helper ────────────────────────────────────────────────────────
/** Fetch JSON from an API path, throwing on non-OK responses. */
export async function apiFetch(path) {
    const res = await fetch(path);
    if (!res.ok)
        throw new Error(`${res.status} ${res.statusText} -- ${path}`);
    return res.json();
}
