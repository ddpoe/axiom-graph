// =============================================================================
// workflow.ts -- Workflow outline view: filter panel + two-panel step explorer
// =============================================================================

import { esc, apiFetch, refreshSvg, collapseAllSvg } from './view-utils.js';
import { ensureMonaco } from './view-utils.js';

// Callbacks to avoid circular imports
let selectNodeFn: (nodeId: string) => void = () => {};
let setViewFn: (view: string) => void = () => {};

export function setWorkflowCallbacks(callbacks: {
  selectNode: (nodeId: string) => void;
  setView: (view: string) => void;
}): void {
  selectNodeFn = callbacks.selectNode;
  setViewFn = callbacks.setView;
}

// ColResize reference (set from app.ts)
let colResizeInit: (container: HTMLElement) => void = () => {};
export function setColResizeInit(fn: (container: HTMLElement) => void): void {
  colResizeInit = fn;
}

// ── State ───────────────────────────────────────────────────────────────────

let _workflows: any[] = [];
let _tasks: any[] = [];
let _workflowsLoaded = false;
let _tasksLoaded = false;
let _available = false;
let _filter: 'workflow' | 'task' = 'workflow';
let _selectedId: any = null;
let _monacoReady = false;
let _monacoEditor: any = null;
let _monacoDecorations: string[] = [];
let _currentSourcePath: string | null = null;
let _pendingSource: { path: string; line: number } | null = null;
let _fHasSteps = false;
let _fHasCritical = false;
let _fLinked = false;
let _groupByMod = true;
let _collapsedGroups = new Set<string>();
let _allCollapsed = true;
let _modSearch = '';
let _modSelected = new Set<string>();
let _refreshInProgress = false;
let _workflowModule: string | null = null;

// ── Public API ──────────────────────────────────────────────────────────────

export function reset(): void {
  _workflows = [];
  _tasks = [];
  _workflowsLoaded = false;
  _tasksLoaded = false;
  _available = false;
  _selectedId = null;
  _modSearch = '';
  _modSelected = new Set();
}

export async function load(): Promise<void> {
  const alreadyLoaded = _filter === 'workflow' ? _workflowsLoaded : _tasksLoaded;
  if (alreadyLoaded) { render(); return; }
  const savedId = sessionStorage.getItem('cortex-workflow-id');
  if (savedId && !_selectedId) _selectedId = parseInt(savedId, 10) || savedId;
  await fetchCurrent();
}

// ── Internal ────────────────────────────────────────────────────────────────

async function fetchCurrent(): Promise<void> {
  const container = document.getElementById('workflow-view');
  if (!container) return;
  const label = _filter === 'task' ? 'tasks' : 'workflows';
  container.innerHTML = `<div class="wf-status">Loading ${label}\u2026</div>`;
  try {
    if (_filter === 'workflow') {
      const data = await apiFetch('/api/workflows');
      _available = data.available;
      _workflows = data.workflows || [];
      _workflowsLoaded = true;
    } else {
      const data = await apiFetch('/api/tasks');
      _available = data.available;
      _tasks = data.tasks || [];
      _tasksLoaded = true;
    }
    _modSelected.clear();
    render();
  } catch (err: any) {
    if (container) container.innerHTML =
      `<div class="wf-status wf-error">Failed to load ${label}: ${err.message}</div>`;
  }
}

async function setRole(role: 'workflow' | 'task'): Promise<void> {
  if (_filter === role) return;
  _filter = role;
  _selectedId = null;
  _modSelected.clear();
  const loaded = role === 'workflow' ? _workflowsLoaded : _tasksLoaded;
  if (loaded) { render(); } else { await fetchCurrent(); }
}

async function refreshWorkflows(): Promise<void> {
  if (_refreshInProgress) return;
  _refreshInProgress = true;
  const btn = document.getElementById('wf-refresh-btn') as HTMLButtonElement | null;
  if (btn) { btn.classList.add('tests-refresh-spinning'); btn.disabled = true; }
  try {
    const resp = await fetch('/api/tests/refresh', { method: 'POST' });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      throw new Error(err.detail || 'Refresh failed');
    }
    _workflowsLoaded = false;
    _tasksLoaded = false;
    await fetchCurrent();
  } catch (err) {
    console.error('Workflow refresh failed:', err);
  } finally {
    _refreshInProgress = false;
    if (btn) { btn.classList.remove('tests-refresh-spinning'); btn.disabled = false; }
  }
}

function rawItems(): any[] {
  return _filter === 'task' ? _tasks : _workflows;
}

function applyFilters(items: any[]): any[] {
  return items.filter(w => {
    if (_fHasSteps && w.step_count === 0) return false;
    if (_fHasCritical && !w.critical) return false;
    if (_fLinked && !w.cortex_node_id) return false;
    if (_modSelected.size > 0 && !_modSelected.has(w.module)) return false;
    return true;
  });
}

function render(): void {
  if (_monacoEditor) {
    _monacoEditor.dispose();
    _monacoEditor = null;
    _currentSourcePath = null;
    _monacoDecorations = [];
  }
  const container = document.getElementById('workflow-view');
  if (!container) return;
  const items = rawItems();
  const label = _filter === 'task' ? 'tasks' : 'workflows';

  if (!_available || items.length === 0) {
    container.innerHTML = `
      <div class="wf-layout">
        <div class="wf-filter-panel"></div>
        <div class="wf-sidebar">
          ${roleToggleHtml()}
          <div class="wf-list"></div>
        </div>
        <div class="wf-detail">
          <div class="wf-steps-col">
            <div class="wf-status wf-empty">
              <p>No ${label} found.</p>
              <p>Run <code>axiom-graph build</code> inside this project
                 to index annotated workflows.</p>
            </div>
          </div>
          <div class="wf-source-col">
            <div class="wf-source-placeholder">No source available</div>
          </div>
        </div>
      </div>`;
    attachRoleToggle();
    return;
  }

  container.innerHTML = `
    <div class="wf-layout">
      <div class="wf-filter-panel" id="wf-filter-panel"></div>
      <div class="col-resize-handle"></div>
      <div class="wf-sidebar">
        <div class="doc-sidebar-header">
          <span>Workflows</span>
          <div class="doc-sidebar-actions">
            <button class="doc-action-btn" id="wf-collapse-btn" title="Expand All" style="transition:transform .2s">${collapseAllSvg}</button>
            <button class="doc-action-btn" id="wf-refresh-btn"
                    title="Refresh: run axiom-graph build to discover new workflows">
              ${refreshSvg}
            </button>
          </div>
        </div>
        ${roleToggleHtml()}
        <div class="wf-list" id="wf-list"></div>
      </div>
      <div class="col-resize-handle"></div>
      <div class="wf-detail" id="wf-detail">
        <div class="wf-steps-col" id="wf-steps-col">
          <div class="wf-status">&#8592; Select a ${label.slice(0, -1)}</div>
        </div>
        <div class="col-resize-handle" data-resize-target="next"></div>
        <div class="wf-source-col" id="wf-source-col">
          <div class="wf-source-placeholder">Select a workflow to view source</div>
        </div>
      </div>
    </div>`;

  attachRoleToggle();
  const filterPanel = document.getElementById('wf-filter-panel');
  if (filterPanel) buildFilterPanel(filterPanel);
  updateList();
  colResizeInit(container);

  const refreshBtn = container.querySelector('#wf-refresh-btn');
  if (refreshBtn) refreshBtn.addEventListener('click', () => refreshWorkflows());
  const collapseBtn = container.querySelector('#wf-collapse-btn');
  if (collapseBtn) collapseBtn.addEventListener('click', () => _toggleCollapseAll());
}

function roleToggleHtml(): string {
  return `
    <div class="wf-role-toggle" id="wf-role-toggle">
      <button class="wf-role-btn${_filter === 'workflow' ? ' active' : ''}" data-role="workflow">Workflows</button>
      <button class="wf-role-btn${_filter === 'task' ? ' active' : ''}" data-role="task">Tasks</button>
    </div>`;
}

function attachRoleToggle(): void {
  const toggle = document.getElementById('wf-role-toggle');
  if (!toggle) return;
  toggle.addEventListener('click', e => {
    const btn = (e.target as HTMLElement).closest('[data-role]') as HTMLElement | null;
    if (btn) setRole(btn.dataset.role as 'workflow' | 'task');
  });
}

function buildFilterPanel(panel: HTMLElement): void {
  const items = rawItems();
  const modCount = new Map<string, number>();
  for (const w of items) modCount.set(w.module, (modCount.get(w.module) || 0) + 1);
  const modules = [...modCount.entries()].sort((a, b) => a[0].localeCompare(b[0]));

  const quickFilters = [
    { key: '_fHasSteps', label: 'Has steps', checked: _fHasSteps },
    { key: '_fHasCritical', label: 'Has \u26a0 critical', checked: _fHasCritical },
    { key: '_fLinked', label: 'Linked to graph', checked: _fLinked },
  ];
  const quickHtml = quickFilters.map(f =>
    `<label class="wf-fp-check"><input type="checkbox" data-key="${f.key}"${f.checked ? ' checked' : ''}>${f.label}</label>`,
  ).join('');

  const groupHtml = `<div class="wf-fp-radio-group">
    <label class="wf-fp-radio"><input type="radio" name="wf-group" value="module" checked>Module</label>
  </div>`;

  const filteredMods = modules.filter(([path]) => !_modSearch || modDisplayName(path).toLowerCase().includes(_modSearch.toLowerCase()));
  const allSelected = _modSelected.size === 0;
  const modItemsHtml = filteredMods.map(([path, count]) => {
    const checked = allSelected || _modSelected.has(path);
    return `<label class="wf-fp-mod-item" title="${esc(path)}">
      <input type="checkbox" data-mod="${esc(path)}"${checked ? ' checked' : ''}>
      <span class="wf-fp-mod-label">${esc(modDisplayName(path))}</span>
      <span class="wf-fp-mod-count">${count}</span></label>`;
  }).join('');

  const hasActive = _fHasSteps || _fHasCritical || _fLinked || _modSelected.size > 0;

  panel.innerHTML = `
    <div class="wf-fp-section"><div class="wf-fp-heading">Show only</div>${quickHtml}</div>
    <div class="wf-fp-section"><div class="wf-fp-heading">Group by</div>${groupHtml}</div>
    <div class="wf-fp-section"><div class="wf-fp-heading">Modules (${modules.length})</div>
      ${modules.length > 6 ? `<input type="text" class="wf-fp-mod-search" id="wf-mod-search" placeholder="Filter modules\u2026" value="${esc(_modSearch)}">` : ''}
      <div class="wf-fp-mod-list" id="wf-mod-list">${modItemsHtml}</div>
      ${hasActive ? '<button class="wf-fp-clear" id="wf-fp-clear">Clear filters</button>' : ''}
    </div>`;

  // Events
  panel.querySelectorAll('input[data-key]').forEach(cb => {
    cb.addEventListener('change', () => {
      const key = (cb as HTMLElement).dataset.key;
      const checked = (cb as HTMLInputElement).checked;
      if (key === '_fHasSteps') _fHasSteps = checked;
      else if (key === '_fHasCritical') _fHasCritical = checked;
      else if (key === '_fLinked') _fLinked = checked;
      updateList();
      _refreshClearBtn(panel);
    });
  });
  panel.querySelectorAll('input[name="wf-group"]').forEach(rb => {
    rb.addEventListener('change', () => {
      _groupByMod = (rb as HTMLInputElement).value === 'module';
      _collapsedGroups.clear();
      _allCollapsed = true;
      updateList();
    });
  });
  const modSearch = panel.querySelector('#wf-mod-search') as HTMLInputElement | null;
  if (modSearch) {
    modSearch.addEventListener('input', () => { _modSearch = modSearch.value; rebuildModList(panel, modules); });
  }
  attachModListEvents(panel, modules);
  const clearBtn = panel.querySelector('#wf-fp-clear');
  if (clearBtn) clearBtn.addEventListener('click', () => _clearFilters(panel));
}

function attachModListEvents(panel: HTMLElement, modules: [string, number][]): void {
  const modList = panel.querySelector('#wf-mod-list');
  if (!modList) return;
  modList.addEventListener('change', e => {
    const cb = (e.target as HTMLElement).closest('input[data-mod]') as HTMLInputElement | null;
    if (!cb) return;
    const path = cb.dataset.mod || '';
    if (_modSelected.size === 0) modules.forEach(([p]) => _modSelected.add(p));
    if (cb.checked) _modSelected.add(path);
    else _modSelected.delete(path);
    if (_modSelected.size === modules.length) _modSelected.clear();
    updateList();
    _refreshClearBtn(panel);
  });
}

function _clearFilters(panel: HTMLElement): void {
  _fHasSteps = _fHasCritical = _fLinked = false;
  _modSelected.clear();
  buildFilterPanel(panel);
  updateList();
}

function _refreshClearBtn(panel: HTMLElement): void {
  const hasActive = _fHasSteps || _fHasCritical || _fLinked || _modSelected.size > 0;
  const existing = panel.querySelector('#wf-fp-clear');
  if (hasActive && !existing) {
    const section = panel.querySelector('.wf-fp-section:last-child');
    if (section) {
      const btn = document.createElement('button');
      btn.className = 'wf-fp-clear';
      btn.id = 'wf-fp-clear';
      btn.textContent = 'Clear filters';
      btn.addEventListener('click', () => _clearFilters(panel));
      section.appendChild(btn);
    }
  } else if (!hasActive && existing) {
    existing.remove();
  }
}

function rebuildModList(panel: HTMLElement, modules: [string, number][]): void {
  const modList = panel.querySelector('#wf-mod-list');
  if (!modList) return;
  const filteredMods = modules.filter(([path]) => !_modSearch || modDisplayName(path).toLowerCase().includes(_modSearch.toLowerCase()));
  const allSelected = _modSelected.size === 0;
  modList.innerHTML = filteredMods.map(([path, count]) => {
    const checked = allSelected || _modSelected.has(path);
    return `<label class="wf-fp-mod-item" title="${esc(path)}">
      <input type="checkbox" data-mod="${esc(path)}"${checked ? ' checked' : ''}>
      <span class="wf-fp-mod-label">${esc(modDisplayName(path))}</span>
      <span class="wf-fp-mod-count">${count}</span></label>`;
  }).join('');
  attachModListEvents(panel, modules);
}

function updateList(): void {
  const listEl = document.getElementById('wf-list');
  if (!listEl) return;
  const filtered = applyFilters(rawItems());
  listEl.innerHTML = '';
  if (filtered.length === 0) {
    listEl.innerHTML = '<div style="padding:12px 14px;font-size:12px;color:#888;">No results match filters.</div>';
    return;
  }
  if (_groupByMod) {
    const groups = new Map<string, any[]>();
    for (const w of filtered) {
      const key = w.module || '';
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key)!.push(w);
    }
    // On first render (allCollapsed + empty set), populate collapsed set
    if (_allCollapsed && _collapsedGroups.size === 0) {
      for (const key of groups.keys()) _collapsedGroups.add(key);
    }
    for (const [modPath, groupItems] of groups) {
      const collapsed = _collapsedGroups.has(modPath);
      listEl.appendChild(_makeGroupHeader(modDisplayName(modPath), modPath, groupItems, collapsed));
      if (!collapsed) {
        for (const wf of groupItems) listEl.appendChild(makeListItem(wf));
      }
    }
  } else {
    for (const wf of filtered) listEl.appendChild(makeListItem(wf));
  }

  _syncCollapseBtn();
  const toSelect = filtered.find(w => w.id === _selectedId) || filtered[0];
  if (toSelect) selectWorkflow(toSelect);
}

function makeListItem(wf: any): HTMLElement {
  const item = document.createElement('div');
  item.className = 'wf-list-item';
  item.dataset.wfId = String(wf.id);
  const badges = [`<span class="wf-list-badge">${wf.step_count}</span>`];
  if (wf.cortex_node_id) badges.push('<span class="wf-linked" title="Linked to cortex node">&#10003;</span>');
  item.innerHTML = `<div class="wf-list-row"><span class="wf-list-name">${esc(wf.name)}</span>${badges.join('')}</div>` +
    (wf.purpose ? `<div class="wf-list-purpose">${esc(wf.purpose)}</div>` : '');
  item.addEventListener('click', () => selectWorkflow(wf));
  return item;
}

async function selectWorkflow(wf: any): Promise<void> {
  _selectedId = wf.id;
  sessionStorage.setItem('cortex-workflow-id', String(wf.id));
  document.querySelectorAll('.wf-list-item').forEach(el => {
    el.classList.toggle('active', (el as HTMLElement).dataset.wfId === String(wf.id));
  });
  const stepsCol = document.getElementById('wf-steps-col');
  if (!stepsCol) return;
  stepsCol.innerHTML = '<div class="wf-status">Loading steps\u2026</div>';
  try {
    const data = await apiFetch(`/api/workflow/${wf.id}/steps`);
    renderDetail(stepsCol, data, wf);
    if (data.func.module) openSource(data.func.module, data.func.line_start || 1);
  } catch (err: any) {
    stepsCol.innerHTML = `<div class="wf-status wf-error">Failed to load steps: ${err.message}</div>`;
  }
}

function renderDetail(stepsCol: HTMLElement, data: any, wf: any): void {
  const func = data.func;
  const steps = data.steps || [];
  _workflowModule = func.module || null;

  let html = '<div class="wf-detail-inner">';
  html += `<div class="wf-detail-header"><div class="wf-detail-title">${esc(func.name)}</div>`;
  if (wf.module) html += `<div class="wf-detail-module">${esc(wf.module)}</div>`;
  if (func.purpose) html += `<div class="wf-detail-purpose">${esc(func.purpose)}</div>`;
  const funcMeta: string[] = [];
  if (func.inputs) funcMeta.push(`<span class="wf-meta-chip">in: ${esc(func.inputs)}</span>`);
  if (func.outputs) funcMeta.push(`<span class="wf-meta-chip">out: ${esc(func.outputs)}</span>`);
  if (funcMeta.length) html += `<div class="wf-func-meta">${funcMeta.join('')}</div>`;
  if (func.critical) html += `<div class="wf-critical">&#9888; ${esc(func.critical)}</div>`;
  if (wf.cortex_node_id) {
    html += `<button class="wf-jump-btn" data-node-id="${esc(wf.cortex_node_id)}">&#8594; View in graph</button>`;
  }
  html += '</div>';

  if (steps.length === 0) {
    html += '<div class="wf-no-steps">No steps recorded for this workflow.</div>';
  } else {
    html += '<ol class="wf-steps">';
    for (const step of steps) {
      const isMinor = step.step_number.toString().includes('.');
      html += `<li class="wf-step${isMinor ? ' wf-step-minor' : ''}" data-line="${step.line || ''}">`;
      html += `<div class="wf-step-num">${esc(step.step_number)}</div><div class="wf-step-body">`;
      html += `<div class="wf-step-name">${esc(step.name)}${step.is_auto ? '<span class="wf-badge wf-badge-auto">auto</span>' : ''}</div>`;
      if (step.purpose) html += `<div class="wf-step-purpose">${esc(step.purpose)}</div>`;
      const stepMeta: string[] = [];
      if (step.inputs) stepMeta.push(`in: ${esc(step.inputs)}`);
      if (step.outputs) stepMeta.push(`out: ${esc(step.outputs)}`);
      if (stepMeta.length) html += `<div class="wf-step-meta">${stepMeta.join(' &middot; ')}</div>`;
      if (step.critical) html += `<div class="wf-step-critical">&#9888; ${esc(step.critical)}</div>`;
      if (step.cortex_node_id) {
        html += `<div class="wf-step-actions">
          <button class="wf-step-link" data-node-id="${esc(step.cortex_node_id)}">&#x229e; Graph</button>
          <button class="wf-step-link wf-step-source-link" data-path="${esc(step.cortex_location || '')}" data-line="${step.cortex_line_start || 1}">&lt;&gt; Source</button>
        </div>`;
      } else if (step.calls_function) {
        html += `<span class="wf-step-calls">${esc(step.calls_function)}</span>`;
      }
      html += '</div></li>';
    }
    html += '</ol>';
  }
  html += '</div>';
  stepsCol.innerHTML = html;

  // Wire events
  stepsCol.querySelectorAll('.wf-step[data-line]').forEach(li => {
    const line = parseInt((li as HTMLElement).dataset.line || '', 10);
    if (!line) return;
    li.addEventListener('click', () => {
      if (_workflowModule && _currentSourcePath !== _workflowModule) openSource(_workflowModule, line);
      else jumpToLine(line);
    });
  });
  stepsCol.querySelectorAll('.wf-jump-btn, .wf-step-link[data-node-id]:not(.wf-step-source-link)').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const nid = (btn as HTMLElement).dataset.nodeId;
      if (nid) { setViewFn('graph'); selectNodeFn(nid); }
    });
  });
  stepsCol.querySelectorAll('.wf-step-source-link').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const path = (btn as HTMLElement).dataset.path;
      const line = parseInt((btn as HTMLElement).dataset.line || '1', 10);
      if (path) openSource(path, line || 1);
    });
  });
}

async function openSource(path: string, line: number, colId = 'wf-source-col'): Promise<void> {
  const col = document.getElementById(colId);
  if (!col) return;
  let data: any;
  try {
    data = await apiFetch('/api/source?path=' + encodeURIComponent(path));
  } catch (e: any) {
    col.innerHTML = `<div class="wf-source-error">Cannot load source: ${esc(e.message)}</div>`;
    return;
  }
  if (!_monacoReady) {
    col.innerHTML = '<div class="wf-source-placeholder">Monaco editor loading\u2026<br><small>Source will appear automatically once loaded.</small></div>';
    _pendingSource = { path, line };
    return;
  }
  _pendingSource = null;
  const m = (window as any).monaco;
  const lang = path.endsWith('.ipynb') ? 'json' : 'python';
  if (!_monacoEditor) {
    col.innerHTML = '<div id="wf-monaco-container" style="width:100%;height:100%;"></div>';
    _monacoEditor = m.editor.create(document.getElementById('wf-monaco-container'), {
      value: data.content, language: lang, readOnly: true, theme: 'vs-dark',
      minimap: { enabled: true }, scrollBeyondLastLine: false, fontSize: 12,
      lineNumbers: 'on', automaticLayout: true, wordWrap: 'off', rulers: [88],
    });
    _currentSourcePath = path;
  } else if (_currentSourcePath !== path) {
    _monacoEditor.getModel()?.dispose();
    const model = m.editor.createModel(data.content, lang);
    _monacoEditor.setModel(model);
    _currentSourcePath = path;
    _monacoDecorations = [];
  }
  if (line) jumpToLine(line);
}

function jumpToLine(line: number): void {
  if (!_monacoEditor || !line) return;
  const m = (window as any).monaco;
  _monacoEditor.revealLineInCenter(line);
  _monacoDecorations = _monacoEditor.deltaDecorations(
    _monacoDecorations,
    [{ range: new m.Range(line, 1, line, 1), options: { isWholeLine: true, className: 'wf-monaco-highlight' } }],
  );
}

// ── Collapse helpers ───────────────────────────────────────────────────────

function _makeGroupHeader(label: string, groupKey: string, groupItems: any[], collapsed = false): HTMLElement {
  const header = document.createElement('div');
  header.className = 'wf-group-header';
  header.style.cursor = 'pointer';

  const chevron = document.createElement('span');
  chevron.className = 'group-toggle';
  chevron.textContent = collapsed ? '\u25b8' : '\u25be';
  header.appendChild(chevron);

  const labelSpan = document.createElement('span');
  labelSpan.className = 'wf-group-label';
  labelSpan.textContent = `${label} (${groupItems.length})`;
  header.appendChild(labelSpan);

  header.addEventListener('click', () => {
    if (_collapsedGroups.has(groupKey)) _collapsedGroups.delete(groupKey);
    else _collapsedGroups.add(groupKey);
    const totalGroups = new Set(applyFilters(rawItems()).map(w => w.module || '')).size;
    _allCollapsed = _collapsedGroups.size >= totalGroups;
    updateList();
  });

  return header;
}

function _toggleCollapseAll(): void {
  _allCollapsed = !_allCollapsed;
  if (_allCollapsed) {
    const filtered = applyFilters(rawItems());
    for (const w of filtered) _collapsedGroups.add(w.module || '');
  } else {
    _collapsedGroups.clear();
  }
  updateList();
}

function _syncCollapseBtn(): void {
  const btn = document.getElementById('wf-collapse-btn');
  if (!btn) return;
  btn.title = _allCollapsed ? 'Expand All' : 'Collapse All';
  btn.style.transform = _allCollapsed ? 'rotate(0deg)' : 'rotate(180deg)';
}

// ── Helpers ─────────────────────────────────────────────────────────────────

function modDisplayName(path: string): string {
  if (!path) return '(unknown)';
  const p = path.replace(/\\/g, '/');
  const parts = p.split('/');
  const file = parts[parts.length - 1].replace(/\.py$/, '');
  const parent = parts.length > 1 ? parts[parts.length - 2] : '';
  return parent ? `${parent}/${file}` : file;
}

// ── Monaco init ─────────────────────────────────────────────────────────────

export function initMonaco(): void {
  ensureMonaco(() => {
    _monacoReady = true;
    if (_pendingSource) {
      const { path, line } = _pendingSource;
      _pendingSource = null;
      openSource(path, line);
    }
  });
}
