// =============================================================================
// doc-api.ts -- Doc CRUD operations + raw JSON view
//
// Handles fetch/save/create operations and raw JSON source editing.
// =============================================================================

import type { RawDoc, DocListEntry } from './types.js';
import { esc, apiFetch } from './view-utils.js';

// ── State ───────────────────────────────────────────────────────────────────

let _docs: DocListEntry[] = [];
let _docsLoaded = false;
let _rawDoc: RawDoc | null = null;
let _selectedDocId: string | null = null;
let _dirty = false;
let _pendingRenames: Array<{ old_id: string; new_id: string }> = [];
let _knownSubdirs: string[] | null = null;
let _rawMonaco: any = null;

// ── Viz config cache (fetched once from /api/config) ───────────────────────
// docsDirs: list of configured docs roots, POSIX-relative to the project
// root (or absolute when configured outside).  docsDirs[0] is the primary
// write target.  Falls back to ["docs"] if the endpoint is unavailable.
let _docsDirs: string[] | null = null;
let _primaryDocsDir = 'docs';
let _vizConfigPromise: Promise<void> | null = null;

export async function fetchVizConfig(): Promise<void> {
  if (_docsDirs !== null) return;
  if (_vizConfigPromise) return _vizConfigPromise;
  _vizConfigPromise = (async () => {
    try {
      const data = await apiFetch<{ docs_dirs?: string[] }>('/api/config');
      const dirs = Array.isArray(data.docs_dirs) && data.docs_dirs.length > 0
        ? data.docs_dirs
        : ['docs'];
      _docsDirs = dirs;
      _primaryDocsDir = dirs[0];
    } catch {
      _docsDirs = ['docs'];
      _primaryDocsDir = 'docs';
    }
  })();
  return _vizConfigPromise;
}

export function getDocsDirs(): string[] { return _docsDirs ?? ['docs']; }
export function getPrimaryDocsDir(): string { return _primaryDocsDir; }

// ── Getters / setters ───────────────────────────────────────────────────────

export function getDocs(): DocListEntry[] { return _docs; }
export function isDocsLoaded(): boolean { return _docsLoaded; }
export function getSelectedDocId(): string | null { return _selectedDocId; }
export function setSelectedDocId(id: string | null): void { _selectedDocId = id; }
export function getRawDoc(): RawDoc | null { return _rawDoc; }
export function setRawDoc(doc: RawDoc | null): void { _rawDoc = doc; }
export function isDirty(): boolean { return _dirty; }
export function setDirty(d: boolean): void { _dirty = d; }
export function getPendingRenames(): Array<{ old_id: string; new_id: string }> { return _pendingRenames; }
export function clearPendingRenames(): void { _pendingRenames = []; }
export function addPendingRename(old_id: string, new_id: string): void { _pendingRenames.push({ old_id, new_id }); }
export function getKnownSubdirs(): string[] | null { return _knownSubdirs; }

// ── Reset ───────────────────────────────────────────────────────────────────

export function reset(): void {
  _docs = [];
  _docsLoaded = false;
  _selectedDocId = null;
  _rawDoc = null;
  _dirty = false;
  _pendingRenames = [];
  _knownSubdirs = null;
  if (_rawMonaco) { _rawMonaco.dispose(); _rawMonaco = null; }
}

// ── Fetch docs list ─────────────────────────────────────────────────────────

export async function fetchDocs(): Promise<DocListEntry[]> {
  const [data] = await Promise.all([
    apiFetch<{ docs: DocListEntry[] }>('/api/docs'),
    fetchVizConfig(),
    fetchSubdirs(),
  ]);
  _docs = data.docs || [];
  _docsLoaded = true;
  return _docs;
}

export async function fetchSubdirs(): Promise<void> {
  if (_knownSubdirs) return;
  try {
    // Server returns {dirs: [...]} — the list contains every subdirectory
    // under every configured docs root (POSIX paths relative to project).
    const data = await apiFetch<{ dirs: string[] }>('/api/docs/subdirs');
    _knownSubdirs = data.dirs || [];
  } catch {
    _knownSubdirs = [];
  }
}

// ── Fetch single doc ────────────────────────────────────────────────────────

export async function fetchDoc(docId: string): Promise<{ raw: RawDoc; rendered: any }> {
  const [rawResp, rendered] = await Promise.all([
    apiFetch<{ doc: RawDoc }>(`/api/docs/${encodeURIComponent(docId)}/raw`),
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

export async function saveDoc(
  docId: string,
  docToSave: RawDoc,
  renames: Array<{ old_id: string; new_id: string }>,
): Promise<void> {
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
    } catch (err) {
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

export async function createDoc(title: string, subdirectory?: string): Promise<{ doc_id: string }> {
  const body: any = { title };
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

export async function createFolder(parentPath: string, name: string): Promise<void> {
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

export async function renameDoc(docId: string, newName: string): Promise<{ new_id: string }> {
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

export async function moveDoc(docId: string, targetDir: string): Promise<{ new_id: string }> {
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

export async function refreshDocs(): Promise<{ resolved_id?: string }> {
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

export function getRawMonaco(): any { return _rawMonaco; }
export function setRawMonaco(editor: any): void { _rawMonaco = editor; }

export function destroyRawMonaco(): void {
  if (_rawMonaco) { _rawMonaco.dispose(); _rawMonaco = null; }
}

// ── Doc directory helper ────────────────────────────────────────────────────

export function docDir(doc: DocListEntry): string {
  // Extract directory from doc ID: "cortex::docs.adrs.015" -> "docs/adrs".
  // When no dotpath is present, fall back to the primary docs root.
  const idParts = doc.id.split('::');
  const dotPath = idParts.length >= 2 ? idParts[1] : doc.id;
  const segments = dotPath.split('.');
  if (segments.length <= 1) return _primaryDocsDir;
  return segments.slice(0, -1).join('/');
}
