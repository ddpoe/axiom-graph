// =============================================================================
// doc-api.ts -- Doc CRUD operations + raw JSON view
//
// Handles fetch/save/create operations and raw JSON source editing.
// =============================================================================
import { apiFetch } from './view-utils.js';
// ── State ───────────────────────────────────────────────────────────────────
let _docs = [];
let _docsLoaded = false;
let _rawDoc = null;
let _selectedDocId = null;
let _dirty = false;
let _pendingRenames = [];
let _knownSubdirs = null;
let _rawMonaco = null;
// ── Viz config cache (fetched once from /api/config) ───────────────────────
// docsDirs: list of configured docs roots, POSIX-relative to the project
// root (or absolute when configured outside).  docsDirs[0] is the primary
// write target.  Falls back to ["docs"] if the endpoint is unavailable.
let _docsDirs = null;
let _primaryDocsDir = 'docs';
let _vizConfigPromise = null;
export async function fetchVizConfig() {
    if (_docsDirs !== null)
        return;
    if (_vizConfigPromise)
        return _vizConfigPromise;
    _vizConfigPromise = (async () => {
        try {
            const data = await apiFetch('/api/config');
            const dirs = Array.isArray(data.docs_dirs) && data.docs_dirs.length > 0
                ? data.docs_dirs
                : ['docs'];
            _docsDirs = dirs;
            _primaryDocsDir = dirs[0];
        }
        catch {
            _docsDirs = ['docs'];
            _primaryDocsDir = 'docs';
        }
    })();
    return _vizConfigPromise;
}
export function getDocsDirs() { return _docsDirs ?? ['docs']; }
export function getPrimaryDocsDir() { return _primaryDocsDir; }
// ── Getters / setters ───────────────────────────────────────────────────────
export function getDocs() { return _docs; }
export function isDocsLoaded() { return _docsLoaded; }
export function getSelectedDocId() { return _selectedDocId; }
export function setSelectedDocId(id) { _selectedDocId = id; }
export function getRawDoc() { return _rawDoc; }
export function setRawDoc(doc) { _rawDoc = doc; }
export function isDirty() { return _dirty; }
export function setDirty(d) { _dirty = d; }
export function getPendingRenames() { return _pendingRenames; }
export function clearPendingRenames() { _pendingRenames = []; }
export function addPendingRename(old_id, new_id) { _pendingRenames.push({ old_id, new_id }); }
export function getKnownSubdirs() { return _knownSubdirs; }
// ── Reset ───────────────────────────────────────────────────────────────────
export function reset() {
    _docs = [];
    _docsLoaded = false;
    _selectedDocId = null;
    _rawDoc = null;
    _dirty = false;
    _pendingRenames = [];
    _knownSubdirs = null;
    if (_rawMonaco) {
        _rawMonaco.dispose();
        _rawMonaco = null;
    }
}
// ── Fetch docs list ─────────────────────────────────────────────────────────
export async function fetchDocs() {
    const [data] = await Promise.all([
        apiFetch('/api/docs'),
        fetchVizConfig(),
        fetchSubdirs(),
    ]);
    _docs = data.docs || [];
    _docsLoaded = true;
    return _docs;
}
export async function fetchSubdirs() {
    if (_knownSubdirs)
        return;
    try {
        // Server returns {dirs: [...]} — the list contains every subdirectory
        // under every configured docs root (POSIX paths relative to project).
        const data = await apiFetch('/api/docs/subdirs');
        _knownSubdirs = data.dirs || [];
    }
    catch {
        _knownSubdirs = [];
    }
}
// ── Fetch single doc ────────────────────────────────────────────────────────
export async function fetchDoc(docId) {
    const [rawResp, rendered] = await Promise.all([
        apiFetch(`/api/docs/${encodeURIComponent(docId)}/raw`),
        apiFetch(`/api/docs/${encodeURIComponent(docId)}/render`),
    ]);
    const raw = rawResp.doc;
    _rawDoc = raw;
    _selectedDocId = docId;
    _dirty = false;
    _pendingRenames = [];
    return { raw, rendered };
}
// ── Save doc ────────────────────────────────────────────────────────────────
export async function saveDoc(docId, docToSave, renames) {
    // Apply renames first (warn on failure but proceed with save)
    if (renames.length > 0) {
        try {
            const renameRes = await fetch(`/api/docs/${encodeURIComponent(docId)}/rename-sections`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ renames }),
            });
            if (!renameRes.ok) {
                const errData = await renameRes.json().catch(() => ({}));
                console.warn('[doc-api] rename-sections failed:', errData.detail || renameRes.statusText);
            }
        }
        catch (err) {
            console.warn('[doc-api] rename-sections request error:', err);
        }
    }
    const res = await fetch(`/api/docs/${encodeURIComponent(docId)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(docToSave),
    });
    if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.detail || res.statusText);
    }
    _dirty = false;
    _pendingRenames = [];
}
// ── Create / rename / move ──────────────────────────────────────────────────
export async function createDoc(title, subdirectory) {
    const body = { title };
    // Only forward subdirectory when it differs from the primary docs root —
    // otherwise let the server default to its primary root.
    if (subdirectory && subdirectory !== _primaryDocsDir) {
        body.subdirectory = subdirectory;
    }
    const resp = await fetch('/api/docs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.detail || resp.statusText);
    }
    return resp.json();
}
export async function createFolder(parentPath, name) {
    const resp = await fetch('/api/docs/mkdir', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: `${parentPath}/${name}` }),
    });
    if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.detail || resp.statusText);
    }
    _knownSubdirs = null; // Force re-fetch
}
export async function renameDoc(docId, newName) {
    const resp = await fetch(`/api/docs/${encodeURIComponent(docId)}/rename`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ new_name: newName }),
    });
    if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.detail || resp.statusText);
    }
    return resp.json();
}
export async function moveDoc(docId, targetDir) {
    const resp = await fetch(`/api/docs/${encodeURIComponent(docId)}/move`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target_directory: targetDir }),
    });
    if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.detail || resp.statusText);
    }
    return resp.json();
}
export async function refreshDocs() {
    const resp = await fetch('/api/docs/rescan', { method: 'POST' });
    if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.detail || resp.statusText);
    }
    const data = await resp.json();
    _docsLoaded = false;
    await fetchDocs();
    return data;
}
// ── Raw JSON source helpers ─────────────────────────────────────────────────
export function getRawMonaco() { return _rawMonaco; }
export function setRawMonaco(editor) { _rawMonaco = editor; }
export function destroyRawMonaco() {
    if (_rawMonaco) {
        _rawMonaco.dispose();
        _rawMonaco = null;
    }
}
// ── Doc directory helper ────────────────────────────────────────────────────
export function docDir(doc) {
    // Extract directory from doc ID: "cortex::docs.adrs.015" -> "docs/adrs".
    // When no dotpath is present, fall back to the primary docs root.
    const idParts = doc.id.split('::');
    const dotPath = idParts.length >= 2 ? idParts[1] : doc.id;
    const segments = dotPath.split('.');
    if (segments.length <= 1)
        return _primaryDocsDir;
    return segments.slice(0, -1).join('/');
}
