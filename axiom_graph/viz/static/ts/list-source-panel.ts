// =============================================================================
// list-source-panel.ts -- Right-side source panel for the list view
//   Monaco source viewer, doc preview, diff viewer, breadcrumb navigation,
//   focus/diff toggles, close, view-in-graph, and Monaco init.
// =============================================================================

import type { AxiomNode } from './types.js';
import { esc, apiFetch } from './view-utils.js';
import { renderMarkdown as docsRenderMarkdown } from './doc-editor.js';
import { NODE_TYPE_COLORS, NODE_TYPE_DEFAULT, getCy, init as graphInit, loadAll as graphLoadAll } from './graph.js';

// Callbacks to avoid circular imports (set by list.ts via setSourcePanelCallbacks)
let selectNodeFn: (nodeId: string) => void = () => {};
let setViewFn: (view: string) => void = () => {};
let setStatusFn: (msg: string) => void = () => {};
let getAppState: () => any = () => ({});
let getNodes: () => AxiomNode[] = () => [];
let getSelectedIds: () => Set<string> = () => new Set();

export function setSourcePanelCallbacks(callbacks: {
  selectNode: (nodeId: string) => void;
  setView: (view: string) => void;
  setStatus: (msg: string) => void;
  getAppState: () => any;
  getNodes: () => AxiomNode[];
  getSelectedIds: () => Set<string>;
}): void {
  selectNodeFn = callbacks.selectNode;
  setViewFn = callbacks.setView;
  setStatusFn = callbacks.setStatus;
  getAppState = callbacks.getAppState;
  getNodes = callbacks.getNodes;
  getSelectedIds = callbacks.getSelectedIds;
}

// ── State ───────────────────────────────────────────────────────────────────

// Monaco source panel state
let _monacoReady = false;
let _monacoEditor: any = null;
let _monacoDecorations: string[] = [];
let _pendingSource: { nodeId: string } | null = null;
let _currentSourcePath: string | null = null;
let _currentSourceData: any = null;
let _focusMode = false;
let _diffMode = false;
let _diffData: any = null;
let _diffEditor: any = null;

// Doc diff state
let _docDiffData: any = null;
let _docDiffMode = false;

// Doc state
let _docRenderData: any = null;
let _docFocusSection: string | null = null;
let _docShowFull = false;
let _docCurrentId: string | null = null;

// ── Source panel ────────────────────────────────────────────────────────────

export async function openSource(nodeId: string): Promise<void> {
  const col = document.getElementById('list-source-col');
  const titleEl = document.getElementById('list-source-title');
  const monacoWrap = document.getElementById('list-monaco-wrap');
  const docWrap = document.getElementById('list-doc-wrap');
  if (col) col.classList.remove('hidden');
  if (monacoWrap) monacoWrap.classList.remove('hidden');
  if (docWrap) docWrap.classList.add('hidden');
  if (titleEl) titleEl.textContent = 'Loading\u2026';

  _diffMode = false;
  _diffData = null;
  if (_diffEditor) { _diffEditor.dispose(); _diffEditor = null; }

  const focusBtn = document.getElementById('list-source-focus-btn');
  if (focusBtn) { focusBtn.classList.remove('hidden'); focusBtn.textContent = 'Focus'; }
  const docsBtn = document.getElementById('list-source-open-docs');
  if (docsBtn) docsBtn.classList.add('hidden');
  const bcEl = document.getElementById('list-doc-breadcrumb');
  if (bcEl) bcEl.classList.add('hidden');
  const navUp = document.getElementById('list-doc-nav-up');
  if (navUp) navUp.classList.add('hidden');
  const navDown = document.getElementById('list-doc-nav-down');
  if (navDown) navDown.classList.add('hidden');
  const diffBtn = document.getElementById('list-source-diff-btn');
  if (diffBtn) { diffBtn.classList.add('hidden'); diffBtn.classList.remove('active'); }

  try {
    const data = await apiFetch(`/api/nodes/${encodeURIComponent(nodeId)}/source`);
    _currentSourceData = { ...data, node_id: nodeId };
    if (titleEl) titleEl.textContent = data.path || nodeId;
    if (!_monacoReady) {
      if (monacoWrap) monacoWrap.innerHTML =
        `<div class="wf-source-placeholder">Monaco loading\u2026<br><small>Source will appear once loaded.</small></div>`;
      _pendingSource = { nodeId };
      return;
    }
    _renderMonaco(data);
    _probeDiff(nodeId);
  } catch (err: any) {
    if (titleEl) titleEl.textContent = `Error: ${err.message}`;
  }
}

export async function openSourceWithDiff(nodeId: string, sha: string): Promise<void> {
  await openSource(nodeId);
  try {
    const url = `/api/nodes/${encodeURIComponent(nodeId)}/diff` + (sha ? `?sha=${encodeURIComponent(sha)}` : '');
    const diff = await apiFetch(url);
    if (diff.error) return;
    _diffData = diff;
    _diffMode = true;
    const diffBtn = document.getElementById('list-source-diff-btn');
    if (diffBtn) { diffBtn.classList.remove('hidden'); (diffBtn as HTMLButtonElement).disabled = false; diffBtn.classList.add('active'); }
    _renderDiffMonaco(diff);
  } catch { /* silently fall back to normal source view */ }
}

export async function openDocPreview(nodeId: string): Promise<void> {
  const col = document.getElementById('list-source-col');
  const titleEl = document.getElementById('list-source-title');
  const monacoWrap = document.getElementById('list-monaco-wrap');
  const docWrap = document.getElementById('list-doc-wrap');
  if (col) col.classList.remove('hidden');
  if (monacoWrap) monacoWrap.classList.add('hidden');
  if (docWrap) docWrap.classList.remove('hidden');
  if (titleEl) titleEl.textContent = 'Loading\u2026';

  _docDiffMode = false;
  _docDiffData = null;

  const focusBtn = document.getElementById('list-source-focus-btn');
  if (focusBtn) { focusBtn.classList.remove('hidden'); focusBtn.textContent = 'Full Doc'; focusBtn.classList.remove('active'); }
  const docsBtn = document.getElementById('list-source-open-docs');
  if (docsBtn) docsBtn.classList.remove('hidden');
  const diffBtn = document.getElementById('list-source-diff-btn');
  if (diffBtn) { diffBtn.classList.add('hidden'); diffBtn.classList.remove('active'); }

  _docShowFull = false;

  if (_monacoEditor) { _monacoEditor.dispose(); _monacoEditor = null; _monacoDecorations = []; }

  const parts = nodeId.split('::');
  let docId: string, sectionSlug: string | null;
  if (parts.length >= 3) {
    docId = parts.slice(0, 2).join('::');
    sectionSlug = parts.slice(2).join('::');
  } else {
    docId = nodeId;
    sectionSlug = null;
  }
  _docFocusSection = sectionSlug;
  _docCurrentId = docId;

  try {
    const renderData = await apiFetch(`/api/docs/${encodeURIComponent(docId)}/render`);
    _docRenderData = renderData;
    if (titleEl) titleEl.textContent = renderData.title || docId;
    _renderDocPanel();
    _probeDocDiff(docId);
  } catch {
    try {
      const altDocId = parts.length >= 2 ? parts.slice(1).join('::') : nodeId;
      const altParts = altDocId.split('::');
      const parentDocId = altParts.length >= 2
        ? parts[0] + '::' + altParts[0]
        : parts[0] + '::' + altDocId;
      sectionSlug = altParts.length >= 2 ? altParts.slice(1).join('::') : null;
      _docFocusSection = sectionSlug;
      const renderData = await apiFetch(`/api/docs/${encodeURIComponent(parentDocId)}/render`);
      _docRenderData = renderData;
      if (titleEl) titleEl.textContent = renderData.title || parentDocId;
      _renderDocPanel();
      _probeDocDiff(parentDocId);
    } catch (err2: any) {
      if (titleEl) titleEl.textContent = 'Doc preview';
      if (docWrap) docWrap.innerHTML = `<div class="list-doc-content"><p style="color:#8b949e">Could not load document: ${esc(err2.message)}</p></div>`;
    }
  }
}

export async function openMarkdownPreview(nodeId: string): Promise<void> {
  const col = document.getElementById('list-source-col');
  const titleEl = document.getElementById('list-source-title');
  const monacoWrap = document.getElementById('list-monaco-wrap');
  const docWrap = document.getElementById('list-doc-wrap');
  if (col) col.classList.remove('hidden');
  if (monacoWrap) monacoWrap.classList.add('hidden');
  if (docWrap) docWrap.classList.remove('hidden');
  if (titleEl) titleEl.textContent = 'Loading\u2026';

  const focusBtn = document.getElementById('list-source-focus-btn');
  if (focusBtn) focusBtn.classList.add('hidden');
  const docsBtn = document.getElementById('list-source-open-docs');
  if (docsBtn) docsBtn.classList.add('hidden');

  if (_monacoEditor) { _monacoEditor.dispose(); _monacoEditor = null; _monacoDecorations = []; }

  try {
    const data = await apiFetch(`/api/nodes/${encodeURIComponent(nodeId)}/source`);
    if (titleEl) titleEl.textContent = data.path || nodeId;
    const rendered = docsRenderMarkdown(data.content || '');
    if (docWrap) docWrap.innerHTML = `<div class="list-doc-content">${rendered}</div>`;
  } catch (err: any) {
    if (titleEl) titleEl.textContent = 'Markdown preview';
    if (docWrap) docWrap.innerHTML = `<div class="list-doc-content"><p style="color:#8b949e">Could not load file: ${esc(err.message)}</p></div>`;
  }
  selectNodeFn(nodeId);
}

// ── Doc panel rendering ──────────────────────────────────────────────────────

function _renderDocPanel(): void {
  const docWrap = document.getElementById('list-doc-wrap');
  const data = _docRenderData;
  if (!data || !data.sections || !docWrap) return;

  const renderMd = (content: string) => docsRenderMarkdown(content);

  let html = '<div class="list-doc-content">';
  if (_docFocusSection && !_docShowFull) {
    const sec = data.sections.find((s: any) => s.id === _docFocusSection || s.id.endsWith(_docFocusSection));
    if (sec) {
      html += `<h2>${esc(sec.heading)}</h2>`;
      html += `<div class="doc-section-content">${renderMd(sec.content || '')}</div>`;
      const secDepth = sec.depth || 0;
      const secIdx = data.sections.indexOf(sec);
      for (let i = secIdx + 1; i < data.sections.length; i++) {
        const child = data.sections[i];
        const childDepth = child.depth || 0;
        if (childDepth <= secDepth) break;
        const relDepth = childDepth - secDepth;
        const hLevel = Math.min(2 + relDepth, 6);
        const indent = relDepth * 16;
        html += `<h${hLevel} style="${indent ? 'margin-left:' + indent + 'px' : ''}">${esc(child.heading)}</h${hLevel}>`;
        html += `<div class="doc-section-content" style="${indent ? 'margin-left:' + indent + 'px' : ''}">${renderMd(child.content || '')}</div>`;
      }
    } else {
      html += `<h2>${esc(data.title)}</h2>`;
      for (const s of data.sections) {
        const depth = s.depth || 0;
        const hLevel = Math.min(3 + depth, 6);
        const indent = depth * 16;
        html += `<h${hLevel} style="${indent ? 'margin-left:' + indent + 'px' : ''}">${esc(s.heading)}</h${hLevel}>`;
        html += `<div class="doc-section-content" style="${indent ? 'margin-left:' + indent + 'px' : ''}">${renderMd(s.content || '')}</div>`;
      }
    }
  } else {
    html += `<h2>${esc(data.title)}</h2>`;
    for (const sec of data.sections) {
      const depth = sec.depth || 0;
      const hLevel = Math.min(3 + depth, 6);
      const indent = depth * 16;
      html += `<h${hLevel} style="${indent ? 'margin-left:' + indent + 'px' : ''}">${esc(sec.heading)}</h${hLevel}>`;
      html += `<div class="doc-section-content" style="${indent ? 'margin-left:' + indent + 'px' : ''}">${renderMd(sec.content || '')}</div>`;
    }
  }
  html += '</div>';
  docWrap.innerHTML = html;

  const focusBtn = document.getElementById('list-source-focus-btn');
  if (focusBtn && _docFocusSection) {
    focusBtn.textContent = _docShowFull ? 'Section' : 'Full Doc';
    focusBtn.classList.toggle('active', _docShowFull);
  } else if (focusBtn) {
    focusBtn.classList.add('hidden');
  }
  _renderDocBreadcrumb();
}

// ── Breadcrumb navigation ────────────────────────────────────────────────────

function _renderDocBreadcrumb(): void {
  const bcEl = document.getElementById('list-doc-breadcrumb');
  const upBtn = document.getElementById('list-doc-nav-up');
  const downBtn = document.getElementById('list-doc-nav-down');
  if (!bcEl) return;

  const data = _docRenderData;
  if (!data || !data.sections || !_docFocusSection) {
    bcEl.classList.add('hidden');
    if (upBtn) upBtn.classList.add('hidden');
    if (downBtn) downBtn.classList.add('hidden');
    return;
  }

  const sections = data.sections;
  const currentSec = sections.find((s: any) => s.id === _docFocusSection || s.id.endsWith(_docFocusSection));
  if (!currentSec) {
    bcEl.classList.add('hidden');
    if (upBtn) upBtn.classList.add('hidden');
    if (downBtn) downBtn.classList.add('hidden');
    return;
  }

  const trail: any[] = [];
  let sec = currentSec;
  while (sec) {
    trail.unshift(sec);
    if (sec.parent_id) sec = sections.find((s: any) => s.id === sec.parent_id);
    else break;
  }
  trail.unshift({ heading: data.title, id: null });

  let html = '';
  for (let i = 0; i < trail.length; i++) {
    if (i > 0) html += '<span class="list-bc-sep">\u203a</span>';
    const isLast = i === trail.length - 1;
    if (isLast) {
      html += `<span class="list-bc-current">${esc(trail[i].heading)}</span>`;
    } else {
      html += `<a class="list-bc-link" data-bc-section-id="${esc(trail[i].id || '')}">${esc(trail[i].heading)}</a>`;
    }
  }
  bcEl.innerHTML = html;
  bcEl.classList.remove('hidden');

  bcEl.querySelectorAll('.list-bc-link').forEach(link => {
    link.addEventListener('click', () => {
      const secId = (link as HTMLElement).dataset.bcSectionId;
      if (!secId) { _docFocusSection = null; _docShowFull = true; }
      else { _docFocusSection = secId; _docShowFull = false; }
      if (_docDiffMode) _renderDocDiff();
      else _renderDocPanel();
    });
  });

  const hasParent = currentSec.parent_id != null;
  const hasChildren = sections.some((s: any) => s.parent_id === currentSec.id);
  if (upBtn) upBtn.classList.toggle('hidden', !hasParent);
  if (downBtn) downBtn.classList.toggle('hidden', !hasChildren);
}

// ── Diff probing ─────────────────────────────────────────────────────────────

async function _probeDiff(nodeId: string): Promise<void> {
  const state = getAppState();
  if (!state.sinceFilter) return;
  try {
    let url = `/api/nodes/${encodeURIComponent(nodeId)}/diff`;
    if (state._sinceBaselineSha) url += `?sha=${encodeURIComponent(state._sinceBaselineSha)}`;
    const diff = await apiFetch(url);
    if (_currentSourceData && _currentSourceData.node_id !== nodeId) return;
    const diffBtn = document.getElementById('list-source-diff-btn') as HTMLButtonElement | null;
    if (!diffBtn) return;
    if (diff.error) {
      diffBtn.classList.remove('hidden');
      diffBtn.disabled = true;
      diffBtn.title = diff.reason || 'No baseline available';
    } else {
      _diffData = diff;
      diffBtn.classList.remove('hidden');
      diffBtn.disabled = false;
      diffBtn.title = `Diff against ${(diff.baseline_sha || '').slice(0, 8)} (${(diff.baseline_date || '').slice(0, 10)})`;
    }
  } catch (err) { console.warn('[list] _probeDiff failed:', err); }
}

async function _probeDocDiff(docId: string): Promise<void> {
  const state = getAppState();
  if (!state.sinceFilter) return;
  try {
    let url = `/api/docs/${encodeURIComponent(docId)}/diff`;
    if (state._sinceBaselineSha) url += `?sha=${encodeURIComponent(state._sinceBaselineSha)}`;
    const diff = await apiFetch(url);
    if (_docCurrentId !== docId) return;
    const diffBtn = document.getElementById('list-source-diff-btn') as HTMLButtonElement | null;
    if (!diffBtn) return;
    if (diff.error) {
      diffBtn.classList.remove('hidden');
      diffBtn.disabled = true;
      diffBtn.title = diff.reason || 'No baseline available';
    } else {
      _docDiffData = diff;
      diffBtn.classList.remove('hidden');
      diffBtn.disabled = false;
      diffBtn.title = `Doc diff: ${(diff.baseline_sha || '').slice(0, 8)} \u2194 ${(diff.submodule_sha || 'current').slice(0, 8)}`;
    }
  } catch (err) { console.warn('[list] _probeDocDiff failed:', err); }
}

// ── Doc diff rendering ───────────────────────────────────────────────────────

function _renderDocDiff(): void {
  const docWrap = document.getElementById('list-doc-wrap');
  const diff = _docDiffData;
  if (!diff || !docWrap) return;

  const oldSections = diff.old_sections || [];
  const newSections = diff.new_sections || [];
  const oldMap: Record<string, any> = {};
  for (const s of oldSections) oldMap[s.id] = s;
  const newMap: Record<string, any> = {};
  for (const s of newSections) newMap[s.id] = s;

  const seenIds = new Set<string>();
  const orderedIds: string[] = [];
  for (const s of newSections) { orderedIds.push(s.id); seenIds.add(s.id); }
  for (const s of oldSections) { if (!seenIds.has(s.id)) { orderedIds.push(s.id); seenIds.add(s.id); } }

  const focusSlug = _docFocusSection && !_docShowFull ? _docFocusSection : null;
  const filteredIds = focusSlug
    ? orderedIds.filter(id => id === focusSlug || id.endsWith(focusSlug))
    : orderedIds;

  let html = '<div class="list-doc-content">';
  const DiffLib = (window as any).Diff;

  for (const id of filteredIds) {
    const oldSec = oldMap[id];
    const newSec = newMap[id];
    html += '<div class="doc-diff-section">';

    if (newSec && !oldSec) {
      html += `<div class="doc-diff-heading">${esc(newSec.heading)}<span class="doc-diff-badge new-section">(new section)</span></div>`;
      html += `<div class="doc-diff-content"><ins>${esc(newSec.content || '')}</ins></div>`;
    } else if (oldSec && !newSec) {
      html += `<div class="doc-diff-heading">${esc(oldSec.heading)}<span class="doc-diff-badge removed-section">(removed section)</span></div>`;
      html += `<div class="doc-diff-content"><del>${esc(oldSec.content || '')}</del></div>`;
    } else if (oldSec && newSec) {
      const oldContent = oldSec.content || '';
      const newContent = newSec.content || '';
      if (oldContent === newContent) {
        html += `<div class="doc-diff-heading">${esc(newSec.heading)}<span class="doc-diff-badge unchanged">(unchanged)</span></div>`;
        html += `<div class="doc-diff-unchanged" onclick="this.nextElementSibling.classList.toggle('hidden')">Click to expand</div>`;
        html += `<div class="doc-diff-content hidden">${esc(newContent)}</div>`;
      } else {
        html += `<div class="doc-diff-heading">${esc(newSec.heading)}</div>`;
        html += '<div class="doc-diff-content">';
        if (DiffLib) {
          const parts = DiffLib.diffWords(oldContent, newContent);
          for (const part of parts) {
            const escaped = esc(part.value);
            if (part.added) html += `<ins>${escaped}</ins>`;
            else if (part.removed) html += `<del>${escaped}</del>`;
            else html += escaped;
          }
        } else {
          html += esc(newContent);
        }
        html += '</div>';
      }
    }
    html += '</div>';
  }
  html += '</div>';
  docWrap.innerHTML = html;

  const titleEl = document.getElementById('list-source-title');
  if (titleEl) {
    const baseSha = (diff.baseline_sha || '').slice(0, 8);
    const subSha = (diff.submodule_sha || 'current').slice(0, 8);
    titleEl.textContent = `Doc Diff: ${baseSha} \u2194 ${subSha}`;
  }
}

// ── Monaco rendering ─────────────────────────────────────────────────────────

function _renderMonaco(data: any): void {
  const wrap = document.getElementById('list-monaco-wrap');
  if (!wrap) return;
  const m = (window as any).monaco;
  if (!m) return;
  const lang = (data.path || '').endsWith('.py') ? 'python' : 'javascript';
  const content = _focusMode && data.focus_start && data.focus_end
    ? data.content.split('\n').slice(data.focus_start - 1, data.focus_end).join('\n')
    : data.content;

  if (!_monacoEditor) {
    wrap.innerHTML = '<div id="list-monaco-container" style="width:100%;height:100%;"></div>';
    _monacoEditor = m.editor.create(
      document.getElementById('list-monaco-container'), {
        value: content, language: lang, readOnly: true, theme: 'vs-dark',
        minimap: { enabled: true }, scrollBeyondLastLine: false, fontSize: 12,
        lineNumbers: _focusMode ? ((ln: number) => String(ln + (data.focus_start || 1) - 1)) : 'on',
        automaticLayout: true, wordWrap: 'off', rulers: [88],
      },
    );
    _currentSourcePath = data.path;
  } else {
    _monacoEditor.getModel()?.dispose();
    const model = m.editor.createModel(content, lang);
    _monacoEditor.setModel(model);
    _monacoEditor.updateOptions({
      lineNumbers: _focusMode ? ((ln: number) => String(ln + (data.focus_start || 1) - 1)) : 'on',
    });
    _currentSourcePath = data.path;
    _monacoDecorations = [];
  }
  if (!_focusMode && data.focus_start) _jumpToLine(data.focus_start, data.focus_end);
}

function _jumpToLine(start: number, end?: number): void {
  if (!_monacoEditor || !start) return;
  const m = (window as any).monaco;
  _monacoEditor.revealLineInCenter(start);
  _monacoDecorations = _monacoEditor.deltaDecorations(
    _monacoDecorations,
    [{ range: new m.Range(start, 1, end || start, 1),
       options: { isWholeLine: true, className: 'wf-monaco-highlight' } }],
  );
}

function _renderDiffMonaco(diff: any): void {
  const wrap = document.getElementById('list-monaco-wrap');
  if (!wrap) return;
  const m = (window as any).monaco;
  if (!m) return;
  if (_monacoEditor) { _monacoEditor.dispose(); _monacoEditor = null; }
  if (_diffEditor) { _diffEditor.dispose(); _diffEditor = null; }
  _monacoDecorations = [];

  let headerHtml = '';
  if (diff.commit_subject) {
    const author = diff.commit_author ? esc(diff.commit_author) : '';
    const date = diff.commit_date ? diff.commit_date.slice(0, 10) : '';
    const meta = [author, date].filter(Boolean).join(' \u00b7 ');
    headerHtml =
      `<div class="diff-commit-header">` +
        `<div class="diff-commit-subject">${esc(diff.commit_subject)}</div>` +
        (meta ? `<div class="diff-commit-meta">${meta}</div>` : '') +
      `</div>`;
  }
  wrap.innerHTML = headerHtml + '<div id="list-monaco-container" style="width:100%;flex:1;"></div>';

  const lang = (_currentSourceData && _currentSourceData.path || '').endsWith('.py') ? 'python' : 'javascript';
  const originalModel = m.editor.createModel(diff.old_content, lang);
  const modifiedModel = m.editor.createModel(diff.new_content, lang);

  _diffEditor = m.editor.createDiffEditor(
    document.getElementById('list-monaco-container'), {
      readOnly: true, theme: 'vs-dark', automaticLayout: true,
      renderSideBySide: true, scrollBeyondLastLine: false, fontSize: 12,
    },
  );
  _diffEditor.setModel({ original: originalModel, modified: modifiedModel });

  const titleEl = document.getElementById('list-source-title');
  const sha = (diff.baseline_sha || '').slice(0, 8);
  const date = (diff.baseline_date || '').slice(0, 10);
  if (titleEl) titleEl.textContent = `Diff: ${sha} (${date}) \u2194 current`;
}

// ── Toggle controls ──────────────────────────────────────────────────────────

export function toggleFocus(): void {
  const monacoWrap = document.getElementById('list-monaco-wrap');
  if (monacoWrap && monacoWrap.classList.contains('hidden') && _docRenderData) {
    _docShowFull = !_docShowFull;
    if (_docDiffMode) _renderDocDiff();
    else _renderDocPanel();
    const focusBtn = document.getElementById('list-source-focus-btn');
    if (focusBtn && _docFocusSection) {
      focusBtn.textContent = _docShowFull ? 'Section' : 'Full Doc';
      focusBtn.classList.toggle('active', _docShowFull);
    }
    return;
  }
  if (_diffMode) { toggleDiff(); return; }
  _focusMode = !_focusMode;
  const btn = document.getElementById('list-source-focus-btn');
  if (btn) btn.classList.toggle('active', _focusMode);
  if (_currentSourceData) _renderMonaco(_currentSourceData);
}

export function toggleDiff(): void {
  const monacoWrap = document.getElementById('list-monaco-wrap');
  const isDocMode = monacoWrap && monacoWrap.classList.contains('hidden') && _docRenderData;
  if (isDocMode) {
    if (!_docDiffData) return;
    _docDiffMode = !_docDiffMode;
    const diffBtn = document.getElementById('list-source-diff-btn');
    if (diffBtn) diffBtn.classList.toggle('active', _docDiffMode);
    if (_docDiffMode) _renderDocDiff();
    else {
      _renderDocPanel();
      const titleEl = document.getElementById('list-source-title');
      if (titleEl && _docRenderData) titleEl.textContent = _docRenderData.title || _docCurrentId || 'Doc';
    }
    return;
  }
  if (!_diffData) return;
  _diffMode = !_diffMode;
  const diffBtn = document.getElementById('list-source-diff-btn');
  if (diffBtn) diffBtn.classList.toggle('active', _diffMode);
  if (_diffMode) {
    _renderDiffMonaco(_diffData);
  } else {
    if (_diffEditor) { _diffEditor.dispose(); _diffEditor = null; }
    if (_monacoEditor) { _monacoEditor.dispose(); _monacoEditor = null; }
    _monacoDecorations = [];
    if (_currentSourceData) _renderMonaco(_currentSourceData);
  }
}

export function closeSource(): void {
  const col = document.getElementById('list-source-col');
  if (col) col.classList.add('hidden');
  if (_diffEditor) { _diffEditor.dispose(); _diffEditor = null; }
  if (_monacoEditor) { _monacoEditor.dispose(); _monacoEditor = null; _monacoDecorations = []; _currentSourcePath = null; }
  _diffMode = false;
  _diffData = null;
  _docDiffMode = false;
  _docDiffData = null;
}

export function viewInGraph(): void {
  const state = getAppState();
  const selectedIds = getSelectedIds();
  const nodes = getNodes();
  const ids = selectedIds.size > 0
    ? [...selectedIds]
    : nodes.map(n => n.id);
  if (ids.length === 0) return;
  const nodeSet = new Set(ids);
  const subNodes = state.allNodes.filter((n: AxiomNode) => nodeSet.has(n.id));
  const subEdges = state.allEdges.filter((e: any) => nodeSet.has(e.from_id) && nodeSet.has(e.to_id));
  if (!getCy()) graphInit(document.getElementById('cy')!);
  graphLoadAll(subNodes, subEdges, state.stalenessMap, state.layout, state.stepEdgeLabels);
  setViewFn('graph');
  setStatusFn(`Graph: ${subNodes.length} nodes from list ${selectedIds.size > 0 ? 'selection' : 'filter'}`);
}

export function getDocCurrentId(): string | null { return _docCurrentId; }

// ── Navigation helpers ───────────────────────────────────────────────────────

function _navigateDocUp(): void {
  const data = _docRenderData;
  if (!data || !data.sections || !_docFocusSection) return;
  const currentSec = data.sections.find((s: any) => s.id === _docFocusSection || s.id.endsWith(_docFocusSection));
  if (!currentSec || !currentSec.parent_id) return;
  _docFocusSection = currentSec.parent_id;
  _docShowFull = false;
  if (_docDiffMode) _renderDocDiff();
  else _renderDocPanel();
}

function _navigateDocDown(): void {
  const data = _docRenderData;
  if (!data || !data.sections || !_docFocusSection) return;
  const currentSec = data.sections.find((s: any) => s.id === _docFocusSection || s.id.endsWith(_docFocusSection));
  if (!currentSec) return;
  const firstChild = data.sections.find((s: any) => s.parent_id === currentSec.id);
  if (!firstChild) return;
  _docFocusSection = firstChild.id;
  _docShowFull = false;
  if (_docDiffMode) _renderDocDiff();
  else _renderDocPanel();
}

// ── VSCode URI helper ────────────────────────────────────────────────────────

export function vscodeUri(location: string): string {
  const match = location.match(/^(.+?)(?:#L(\d+))?(?:-L\d+)?$/);
  if (!match) return '#';
  const relPath = match[1];
  const line = match[2] || '1';
  const state = getAppState();
  const root = (state.meta && state.meta.project_root) || '';
  const sep = root.includes('\\') ? '\\' : '/';
  const abs = root ? (root + sep + relPath.replace(/\//g, sep)) : relPath;
  return `vscode://file/${abs}:${line}`;
}

// ── Event binding (called from list.ts bindSortHandlers) ─────────────────────

export function bindSourcePanelHandlers(): void {
  const focusBtn = document.getElementById('list-source-focus-btn');
  if (focusBtn) focusBtn.addEventListener('click', () => toggleFocus());
  const diffBtn = document.getElementById('list-source-diff-btn');
  if (diffBtn) diffBtn.addEventListener('click', () => toggleDiff());
  const closeBtn = document.getElementById('list-source-close');
  if (closeBtn) closeBtn.addEventListener('click', () => closeSource());
  const openDocsBtn = document.getElementById('list-source-open-docs');
  if (openDocsBtn) {
    openDocsBtn.addEventListener('click', () => {
      if (_docCurrentId) {
        sessionStorage.setItem('cortex-doc-id', _docCurrentId);
        setViewFn('docs');
      }
    });
  }

  const navUpBtn = document.getElementById('list-doc-nav-up');
  if (navUpBtn) navUpBtn.addEventListener('click', () => _navigateDocUp());
  const navDownBtn = document.getElementById('list-doc-nav-down');
  if (navDownBtn) navDownBtn.addEventListener('click', () => _navigateDocDown());
}

// ── Monaco init ─────────────────────────────────────────────────────────────

export function initMonaco(): void {
  const req = (window as any).require;
  if (typeof req === 'undefined') return;
  req.config({ paths: { vs: 'https://cdnjs.cloudflare.com/ajax/libs/monaco-editor/0.52.0/min/vs' } });
  req(['vs/editor/editor.main'], () => {
    _monacoReady = true;
    if (_pendingSource) {
      const { nodeId } = _pendingSource;
      _pendingSource = null;
      openSource(nodeId);
    }
  });
}
