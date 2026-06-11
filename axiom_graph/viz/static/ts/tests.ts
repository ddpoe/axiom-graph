// =============================================================================
// tests.ts -- Tests outline view: cortex-native, unified endpoint
//
// Layout: [filter panel] | [list panel] | [detail + Monaco]
// =============================================================================

import { esc, apiFetch, refreshSvg, collapseAllSvg } from './view-utils.js';
import { ensureMonaco } from './view-utils.js';

// Callbacks
let selectNodeFn: (nodeId: string) => void = () => {};
let setViewFn: (view: string) => void = () => {};

export function setTestsCallbacks(callbacks: {
  selectNode: (nodeId: string) => void;
  setView: (view: string) => void;
}): void {
  selectNodeFn = callbacks.selectNode;
  setViewFn = callbacks.setView;
}

let colResizeInit: (container: HTMLElement) => void = () => {};
export function setColResizeInit(fn: (container: HTMLElement) => void): void {
  colResizeInit = fn;
}

// ── State ───────────────────────────────────────────────────────────────────

let _tests: any[] = [];
let _testsLoaded = false;
let _available = false;
let _fixtures: Record<string, any> = {};
let _selectedId: string | null = null;
let _fHasSteps = false;
let _fHasCritical = false;
let _fIsSkeleton = false;
let _fHasCovers = false;
let _fLinked = false;
let _fHideT1 = false;
let _groupBy = 'module';
let _collapsedGroups = new Set<string>();
let _allCollapsed = true;
let _modSearch = '';
let _modSelected = new Set<string>();
let _monacoReady = false;
let _monacoEditor: any = null;
let _monacoDecorations: string[] = [];
let _currentSourcePath: string | null = null;
let _pendingSource: { path: string; line: number } | null = null;
let _refreshInProgress = false;

// ── Public API ──────────────────────────────────────────────────────────────

export function reset(): void {
  _tests = [];
  _testsLoaded = false;
  _available = false;
  _fixtures = {};
  _selectedId = null;
  _modSearch = '';
  _modSelected = new Set();
}

export async function load(): Promise<void> {
  if (_testsLoaded) { render(); return; }
  await fetchTests();
  const savedId = sessionStorage.getItem('cortex-test-id');
  if (savedId && !_selectedId) _selectedId = savedId;
}

async function fetchTests(): Promise<void> {
  const container = document.getElementById('tests-view');
  if (!container) return;
  container.innerHTML = '<div class="wf-status">Loading tests\u2026</div>';
  try {
    const [testsData, fixturesData] = await Promise.all([
      apiFetch('/api/tests'),
      apiFetch('/api/fixtures'),
    ]);
    _available = testsData.available;
    _testsLoaded = true;
    _modSelected.clear();
    _tests = (testsData.tests || []).map((t: any) => ({
      ...t,
      is_t1: t.tier === 'T1',
      covers_count: t.validates_count || 0,
    }));
    _fixtures = {};
    for (const f of (fixturesData.fixtures || [])) _fixtures[f.id] = f;
    render();
  } catch (err: any) {
    if (container) container.innerHTML = `<div class="wf-status wf-error">Failed to load tests: ${esc(err.message)}</div>`;
  }
}

function tier(t: any): string { return t.tier || 'T1'; }
function hasTierDiversity(): boolean { return _tests.some(t => t.tier !== 'T1'); }
async function refreshTests(): Promise<void> {
  if (_refreshInProgress) return;
  _refreshInProgress = true;
  const btn = document.getElementById('tests-refresh-btn') as HTMLButtonElement | null;
  if (btn) { btn.classList.add('tests-refresh-spinning'); btn.disabled = true; }
  try {
    const resp = await fetch('/api/tests/refresh', { method: 'POST' });
    if (!resp.ok) { const err = await resp.json().catch(() => ({ detail: resp.statusText })); throw new Error(err.detail || 'Refresh failed'); }
    _testsLoaded = false;
    await fetchTests();
  } catch (err) { console.error('Tests refresh failed:', err); }
  finally {
    _refreshInProgress = false;
    if (btn) { btn.classList.remove('tests-refresh-spinning'); btn.disabled = false; }
  }
}

function applyFilters(items: any[]): any[] {
  return items.filter(w => {
    if (_fHideT1 && w.is_t1) return false;
    if (_fHasSteps && w.step_count === 0) return false;
    if (_fHasCritical && !w.critical) return false;
    if (_fIsSkeleton && w.critical !== 'NOT IMPLEMENTED') return false;
    if (_fHasCovers && !(w.validates_count > 0)) return false;
    if (_fLinked && !w.cortex_node_id) return false;
    if (_modSelected.size > 0 && !_modSelected.has(w.module)) return false;
    return true;
  });
}

function render(): void {
  if (_monacoEditor) { _monacoEditor.dispose(); _monacoEditor = null; _currentSourcePath = null; _monacoDecorations = []; }
  const container = document.getElementById('tests-view');
  if (!container) return;

  if (!_available || _tests.length === 0) {
    container.innerHTML = `<div class="wf-layout"><div class="wf-filter-panel"></div>
      <div class="wf-sidebar"><div class="wf-list"></div></div>
      <div class="wf-detail"><div class="wf-steps-col"><div class="wf-status wf-empty"><p>No tests found.</p><p>Rebuild the Cortex index to discover tests.</p></div></div>
        <div class="wf-source-col" id="tests-source-col"><div class="wf-source-placeholder">No source available</div></div></div></div>`;
    return;
  }

  container.innerHTML = `<div class="wf-layout">
    <div class="wf-filter-panel" id="tests-filter-panel"></div>
    <div class="col-resize-handle"></div>
    <div class="wf-sidebar">
      <div class="doc-sidebar-header"><span>Tests (${_tests.length})</span>
        <div class="doc-sidebar-actions">
          <button class="doc-action-btn" id="tests-collapse-btn" title="Expand All" style="transition:transform .2s">${collapseAllSvg}</button>
          <button class="doc-action-btn" id="tests-refresh-btn" title="Refresh: rebuild cortex index">${refreshSvg}</button>
        </div>
      </div>
      <div class="wf-list" id="tests-list"></div>
    </div>
    <div class="col-resize-handle"></div>
    <div class="wf-detail" id="tests-detail">
      <div class="wf-steps-col" id="tests-steps-col"><div class="wf-status">&#8592; Select a test</div></div>
      <div class="col-resize-handle" data-resize-target="next"></div>
      <div class="wf-source-col" id="tests-source-col"><div class="wf-source-placeholder">Select a test to view source</div></div>
    </div>
  </div>`;

  const filterPanel = document.getElementById('tests-filter-panel');
  if (filterPanel) buildFilterPanel(filterPanel);
  updateList();
  colResizeInit(container);

  const refreshBtn = container.querySelector('#tests-refresh-btn');
  if (refreshBtn) refreshBtn.addEventListener('click', () => refreshTests());
  const collapseBtn = container.querySelector('#tests-collapse-btn');
  if (collapseBtn) collapseBtn.addEventListener('click', () => _toggleCollapseAll());
}

function buildFilterPanel(panel: HTMLElement): void {
  const showTier = hasTierDiversity();
  const modCount = new Map<string, number>();
  for (const t of _tests) modCount.set(t.module, (modCount.get(t.module) || 0) + 1);
  const modules = [...modCount.entries()].sort((a, b) => a[0].localeCompare(b[0]));

  const quickFilters = [
    ...(showTier ? [{ key: '_fHideT1', label: 'Hide plain T1', checked: _fHideT1 }] : []),
    { key: '_fHasSteps', label: 'Has steps', checked: _fHasSteps },
    { key: '_fHasCritical', label: 'Has \u26a0 critical', checked: _fHasCritical },
    { key: '_fIsSkeleton', label: 'Skeleton only', checked: _fIsSkeleton },
    { key: '_fHasCovers', label: 'Covers nodes', checked: _fHasCovers },
    { key: '_fLinked', label: 'Linked to graph', checked: _fLinked },
  ];
  const quickHtml = quickFilters.map(f =>
    `<label class="wf-fp-check"><input type="checkbox" data-key="${f.key}"${f.checked ? ' checked' : ''}>${f.label}</label>`,
  ).join('');

  const groupHtml = `<div class="wf-fp-radio-group">
    ${['module', ...(showTier ? ['tier'] : [])].map(v => `<label class="wf-fp-radio"><input type="radio" name="test-group" value="${v}"${_groupBy === v ? ' checked' : ''}>${v.charAt(0).toUpperCase() + v.slice(1)}</label>`).join('')}
  </div>`;

  const allSelected = _modSelected.size === 0;
  const modItemsHtml = modules.filter(([path]) => !_modSearch || modDisplayName(path).toLowerCase().includes(_modSearch.toLowerCase())).map(([path, count]) => {
    const checked = allSelected || _modSelected.has(path);
    return `<label class="wf-fp-mod-item" title="${esc(path)}"><input type="checkbox" data-mod="${esc(path)}"${checked ? ' checked' : ''}><span class="wf-fp-mod-label">${esc(modDisplayName(path))}</span><span class="wf-fp-mod-count">${count}</span></label>`;
  }).join('');

  const hasActiveFilters = _fHideT1 || _fHasSteps || _fHasCritical
    || _fIsSkeleton || _fHasCovers || _fLinked
    || _modSelected.size > 0;

  panel.innerHTML = `
    <div class="wf-fp-section"><div class="wf-fp-heading">Show only</div>${quickHtml}</div>
    <div class="wf-fp-section"><div class="wf-fp-heading">Group by</div>${groupHtml}</div>
    <div class="wf-fp-section"><div class="wf-fp-heading">Modules (${modules.length})</div>
      ${modules.length > 6 ? `<input type="text" class="wf-fp-mod-search" placeholder="Filter modules\u2026" value="${esc(_modSearch)}">` : ''}
      <div class="wf-fp-mod-list">${modItemsHtml}</div>
      ${hasActiveFilters ? '<button class="wf-fp-clear" id="tests-fp-clear">Clear filters</button>' : ''}
    </div>`;

  // Events
  panel.querySelectorAll('input[data-key]').forEach(cb => {
    cb.addEventListener('change', () => {
      const key = (cb as HTMLElement).dataset.key;
      const checked = (cb as HTMLInputElement).checked;
      if (key === '_fHideT1') _fHideT1 = checked;
      else if (key === '_fHasSteps') _fHasSteps = checked;
      else if (key === '_fHasCritical') _fHasCritical = checked;
      else if (key === '_fIsSkeleton') _fIsSkeleton = checked;
      else if (key === '_fHasCovers') _fHasCovers = checked;
      else if (key === '_fLinked') _fLinked = checked;
      updateList();
      _refreshClearBtn(panel);
    });
  });
  panel.querySelectorAll('input[name="test-group"]').forEach(rb => {
    rb.addEventListener('change', () => {
      _groupBy = (rb as HTMLInputElement).value;
      _collapsedGroups.clear();
      _allCollapsed = true;
      updateList();
    });
  });
  const modSearchEl = panel.querySelector('.wf-fp-mod-search') as HTMLInputElement | null;
  if (modSearchEl) modSearchEl.addEventListener('input', () => { _modSearch = modSearchEl.value; buildFilterPanel(panel); });
  panel.querySelectorAll('.wf-fp-mod-list').forEach(list => {
    list.addEventListener('change', e => {
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
  });
  const clearBtn = panel.querySelector('#tests-fp-clear');
  if (clearBtn) clearBtn.addEventListener('click', () => _clearFilters(panel));
}

function _clearFilters(panel: HTMLElement): void {
  _fHideT1 = _fHasSteps = _fHasCritical = _fIsSkeleton = _fHasCovers = _fLinked = false;
  _modSelected.clear();
  buildFilterPanel(panel);
  updateList();
}

function _refreshClearBtn(panel: HTMLElement): void {
  const hasActive = _fHideT1 || _fHasSteps || _fHasCritical
    || _fIsSkeleton || _fHasCovers || _fLinked
    || _modSelected.size > 0;
  const existing = panel.querySelector('#tests-fp-clear');
  if (hasActive && !existing) {
    const section = panel.querySelector('.wf-fp-section:last-child');
    if (section) {
      const btn = document.createElement('button');
      btn.className = 'wf-fp-clear';
      btn.id = 'tests-fp-clear';
      btn.textContent = 'Clear filters';
      btn.addEventListener('click', () => _clearFilters(panel));
      section.appendChild(btn);
    }
  } else if (!hasActive && existing) {
    existing.remove();
  }
}

function updateList(): void {
  const listEl = document.getElementById('tests-list');
  if (!listEl) return;
  const filtered = applyFilters(_tests);
  listEl.innerHTML = '';
  if (filtered.length === 0) {
    listEl.innerHTML = '<div style="padding:12px 14px;font-size:12px;color:#888;">No tests match filters.</div>';
    return;
  }

  // Collect all group keys for initial collapsed state
  const allGroupKeys: string[] = [];

  if (_groupBy === 'module') {
    const groups = new Map<string, any[]>();
    for (const t of filtered) {
      const key = t.module || '';
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key)!.push(t);
    }
    for (const key of groups.keys()) allGroupKeys.push(key);
    // On first render (allCollapsed + empty set), populate collapsed set
    if (_allCollapsed && _collapsedGroups.size === 0) {
      for (const k of allGroupKeys) _collapsedGroups.add(k);
    }
    for (const [modPath, groupItems] of groups) {
      const collapsed = _collapsedGroups.has(modPath);
      listEl.appendChild(_makeGroupHeader(modDisplayName(modPath), modPath, groupItems, collapsed));
      if (!collapsed) {
        for (const t of groupItems) listEl.appendChild(makeListItem(t));
      }
    }
  } else if (_groupBy === 'tier') {
    const tierOrder = ['T3', 'T2', 'T1'] as const;
    const tierLabel: Record<string, string> = {
      T3: 'T3 \u2014 Workflow + steps',
      T2: 'T2 \u2014 Workflow entry',
      T1: 'T1 \u2014 Unannotated',
    };
    const groups = new Map<string, any[]>(tierOrder.map(t => [t, []]));
    for (const t of filtered) {
      const k = tier(t);
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k)!.push(t);
    }
    for (const t of tierOrder) {
      if ((groups.get(t) || []).length > 0) allGroupKeys.push(t);
    }
    if (_allCollapsed && _collapsedGroups.size === 0) {
      for (const k of allGroupKeys) _collapsedGroups.add(k);
    }
    for (const t of tierOrder) {
      const groupItems = groups.get(t) || [];
      if (groupItems.length === 0) continue;
      const collapsed = _collapsedGroups.has(t);
      const paths = [...new Set(groupItems.map(w => w.module))] as string[];
      listEl.appendChild(_makeGroupHeader(tierLabel[t], paths.length === 1 ? paths[0] : null, groupItems, collapsed));
      if (!collapsed) {
        for (const w of groupItems) listEl.appendChild(makeListItem(w));
      }
    }
  } else {
    for (const t of filtered) listEl.appendChild(makeListItem(t));
  }

  _syncCollapseBtn();

  const toSelect = filtered.find(t => t.id === _selectedId) || filtered[0];
  if (toSelect) selectTest(toSelect);
}

function makeListItem(t: any): HTMLElement {
  const item = document.createElement('div');
  item.className = 'wf-list-item';
  item.dataset.testId = String(t.id);
  const tierBadge = `<span class="wf-badge wf-badge-${tier(t).toLowerCase()}">${tier(t)}</span>`;
  const skeletonChip = t.critical === 'NOT IMPLEMENTED' ? '<span class="wf-list-skeleton-chip">skeleton</span>' : '';
  const coversChip = t.validates_count > 0 ? `<span class="wf-badge wf-badge-covers">${t.validates_count}</span>` : '';
  const stepChip = t.step_count > 0 ? `<span class="wf-list-badge">${t.step_count}</span>` : '';
  item.innerHTML = `<div class="wf-list-row"><span class="wf-list-name">${esc(t.name)}</span>${tierBadge}${stepChip}${coversChip}${skeletonChip}</div>` +
    (t.purpose ? `<div class="wf-list-purpose">${esc(t.purpose)}</div>` : '');

  item.addEventListener('click', () => selectTest(t));
  return item;
}

async function selectTest(t: any): Promise<void> {
  _selectedId = t.id;
  sessionStorage.setItem('cortex-test-id', String(t.id));
  document.querySelectorAll('.wf-list-item').forEach(el => {
    el.classList.toggle('active', (el as HTMLElement).dataset.testId === String(t.id));
  });
  const stepsCol = document.getElementById('tests-steps-col');
  if (!stepsCol) return;
  stepsCol.innerHTML = '<div class="wf-status">Loading\u2026</div>';

  if (t.is_t1) {
    // T1: no envelope entry -- render inline without an API call
    renderSimpleDetail(stepsCol, t);
    if (t.module) openSource(t.module, t.line_start || 1);
    return;
  }

  // T2/T3: load detail from cortex-native endpoint
  try {
    const cortexId = encodeURIComponent(t.cortex_id || t.id);
    const data = await apiFetch(`/api/test-detail/${cortexId}`);
    // Adapt to renderTestDetail's expected shape
    const detailData = {
      func: {
        name: data.name,
        module: data.module,
        line_start: data.line_start,
        purpose: data.purpose,
        critical: data.critical,
      },
      steps: data.steps || [],
      fixtures: data.fixtures || [],
    };
    // Merge validates into t for display
    t.validates = data.validates || t.validates || [];
    t.validates_count = data.validates_count || t.validates_count || 0;
    renderTestDetail(stepsCol, detailData, t);
    if (data.module) openSource(data.module, data.line_start || 1);
  } catch {
    // Fallback: render with what we have from the unified endpoint
    renderSimpleDetail(stepsCol, t);
    if (t.module) openSource(t.module, t.line_start || 1);
  }
}

function renderTestDetail(stepsCol: HTMLElement, data: any, t: any): void {
  const func = data.func;
  const steps = data.steps || [];
  const tierVal = tier(t);
  const tierCls = tierVal === 'T3' ? 'tier3' : tierVal === 'T2' ? 'tier2' : 'tier1';
  let html = '<div class="wf-detail-inner">';
  html += `<div class="wf-detail-header"><div class="wf-detail-title">${esc(func.name)}</div>`;
  html += `<span class="wf-badge wf-badge-${tierCls}" title="${tierVal}">${tierVal}</span>`;
  if (t.module) html += `<div class="wf-detail-module">${esc(t.module)}</div>`;
  if (func.purpose) html += `<div class="wf-detail-purpose">${esc(func.purpose)}</div>`;
  const funcMeta: string[] = [];
  if (func.inputs) funcMeta.push(`<span class="wf-meta-chip">in: ${esc(func.inputs)}</span>`);
  if (func.outputs) funcMeta.push(`<span class="wf-meta-chip">out: ${esc(func.outputs)}</span>`);
  if (funcMeta.length) html += `<div class="wf-func-meta">${funcMeta.join('')}</div>`;
  if (func.critical) html += `<div class="wf-step-critical${func.critical === 'NOT IMPLEMENTED' ? '--skeleton' : ''}">&#9888; ${esc(func.critical)}</div>`;
  html += '</div>';

  // Steps
  if (steps.length > 0) {
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
      if (step.critical) html += `<div class="wf-step-critical${step.critical === 'NOT IMPLEMENTED' ? '--skeleton' : ''}">&#9888; ${esc(step.critical)}</div>`;
      if (step.cortex_node_id) {
        html += `<div class="wf-step-actions"><button class="wf-step-link" data-node-id="${esc(step.cortex_node_id)}">&#x229e; Graph</button></div>`;
      }
      html += '</div></li>';
    }
    html += '</ol>';
  } else {
    html += '<div class="wf-no-steps">No steps recorded for this test.</div>';
  }

  // Validates / covers
  if (t.validates && t.validates.length > 0) {
    html += `<div class="wf-covers-section"><div class="wf-covers-heading">Validates (${t.validates.length})</div><ul class="wf-covers-list">`;
    for (const nid of t.validates) {
      const dotpath = typeof nid === 'string' ? nid : (nid.dotpath || nid);
      html += `<li class="wf-covers-item"><span class="wf-covers-dotpath">${esc(dotpath)}</span>`;
      html += `<button class="wf-step-link" data-node-id="${esc(dotpath)}">&#8594; graph</button></li>`;
    }
    html += '</ul></div>';
  }

  // Fixtures (from API response)
  const fixtures = data.fixtures || [];
  if (fixtures.length > 0) {
    html += `<div class="wf-fixtures-section"><div class="wf-section-heading">Fixtures (${fixtures.length})</div><ul class="wf-fixtures-list">`;
    for (const fix of fixtures) {
      const nid = fix.cortex_node_id ? esc(fix.cortex_node_id) : '';
      const locText = fix.location
        ? esc(fix.location) + (fix.line_start ? `:${fix.line_start}` : '')
        : '';

      html += `<li class="wf-fixture-item"><div class="wf-fixture-header"><code class="wf-fixture-name">${esc(fix.name)}</code><span class="wf-badge wf-badge-fixture">fixture</span></div>`;
      if (locText) html += `<div class="wf-fixture-loc">${locText}</div>`;
      if (fix.docstring) html += `<div class="wf-fixture-doc">${esc(fix.docstring)}</div>`;

      const actions: string[] = [];
      if (fix.location) {
        actions.push(`<button class="wf-step-link" data-source-path="${esc(fix.location)}" data-source-line="${fix.line_start || 1}">&#x2197; View source</button>`);
      }
      if (nid) {
        actions.push(`<button class="wf-step-link" data-node-id="${nid}">&#8594; Jump to graph</button>`);
      }
      if (actions.length) html += `<div class="wf-fixture-actions">${actions.join('')}</div>`;
      html += '</li>';
    }
    html += '</ul></div>';
  } else if (t.fixture_ids && t.fixture_ids.length > 0) {
    // Fallback: use fixture_ids from unified endpoint
    html += `<div class="wf-fixtures-section"><div class="wf-section-heading">Fixtures (${t.fixture_ids.length})</div>`;
    for (const fid of t.fixture_ids) {
      const f = _fixtures[fid];
      if (f) {
        html += `<div class="wf-fixture-item"><div class="wf-fixture-header"><code class="wf-fixture-name">${esc(f.name)}</code><span class="wf-badge wf-badge-fixture">fixture</span></div>`;
        if (f.location) html += `<div class="wf-fixture-loc">${esc(f.location)}</div>`;
        if (f.docstring) html += `<div class="wf-fixture-doc">${esc(f.docstring)}</div>`;
        html += '</div>';
      }
    }
    html += '</div>';
  }

  // Legacy coverage -- shown when validates is empty but coverage exists
  const coverage = data.coverage || [];
  if (coverage.length > 0 && !(t.validates && t.validates.length > 0)) {
    html += '<div class="wf-covers-section"><div class="wf-covers-heading">Legacy coverage</div><ul class="wf-covers-list">';
    for (const dotpath of coverage) {
      html += `<li class="wf-covers-item"><span class="wf-covers-dotpath">${esc(dotpath)}</span>`;
      html += `<button class="wf-step-link" data-node-id="${esc(dotpath)}">&#8594; graph</button></li>`;
    }
    html += '</ul></div>';
  }

  html += '</div>';
  stepsCol.innerHTML = html;

  // Wire events
  stepsCol.querySelectorAll('.wf-step-link[data-node-id]').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const nid = (btn as HTMLElement).dataset.nodeId;
      if (nid) { setViewFn('graph'); selectNodeFn(nid); }
    });
  });
  stepsCol.querySelectorAll('.wf-step-link[data-source-path]').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const path = (btn as HTMLElement).dataset.sourcePath;
      const line = parseInt((btn as HTMLElement).dataset.sourceLine || '1', 10);
      if (path) openSource(path, line);
    });
  });
  // Step row click -> jump Monaco to that step's source line
  stepsCol.querySelectorAll('.wf-step[data-line]').forEach(li => {
    const line = parseInt((li as HTMLElement).dataset.line || '', 10);
    if (line) li.addEventListener('click', () => jumpToLine(line));
  });
}

function renderSimpleDetail(stepsCol: HTMLElement, t: any): void {
  const tierVal = tier(t);
  const tierCls = tierVal === 'T3' ? 'tier3' : tierVal === 'T2' ? 'tier2' : 'tier1';

  let html = `<div class="wf-detail-inner">
    <div class="wf-detail-header">
      <div class="wf-detail-title">${esc(t.name)}</div>
      <span class="wf-badge wf-badge-${tierCls}" title="${tierVal}">${tierVal}</span>`;

  if (t.module) {
    html += `<div class="wf-detail-module">${esc(t.module)}</div>`;
  }
  if (t.purpose || t.docstring) {
    html += `<div class="wf-detail-purpose">${esc(t.purpose || t.docstring)}</div>`;
  }
  html += '</div>';

  // Validates edges
  if (t.validates && t.validates.length > 0) {
    html += `<div class="wf-covers-section"><div class="wf-covers-heading">Validates (${t.validates.length})</div><ul class="wf-covers-list">`;
    for (const nid of t.validates) {
      html += `<li class="wf-covers-item"><span class="wf-covers-dotpath">${esc(nid)}</span>`;
      html += `<button class="wf-step-link" data-node-id="${esc(nid)}">&#8594; graph</button>`;
      html += '</li>';
    }
    html += '</ul></div>';
  }

  // T1 upgrade hint
  if (t.tier === 'T1') {
    html += `<div class="wf-no-steps"><em>Unannotated test &mdash; no <code>@workflow</code> decorator.</em><br>Add <code>@workflow(purpose=&quot;…&quot;)</code> and re-run <code>axiom-graph build</code> to promote to T2.</div>`;
  }

  // Jump to graph button
  if (t.cortex_node_id) {
    html += `<div style="margin-top:10px"><button class="wf-step-link" data-node-id="${esc(t.cortex_node_id)}">&#8594; Jump to graph</button></div>`;
  }

  html += '</div>';
  stepsCol.innerHTML = html;

  // Wire events
  stepsCol.querySelectorAll('.wf-step-link[data-node-id]').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const nodeId = (btn as HTMLElement).dataset.nodeId;
      if (nodeId) { setViewFn('graph'); selectNodeFn(nodeId); }
    });
  });
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

async function openSource(path: string, line: number): Promise<void> {
  const col = document.getElementById('tests-source-col');
  if (!col) return;
  let data: any;
  try { data = await apiFetch('/api/source?path=' + encodeURIComponent(path)); }
  catch (e: any) { col.innerHTML = `<div class="wf-source-error">Cannot load source: ${esc(e.message)}</div>`; return; }
  if (!_monacoReady) {
    col.innerHTML = '<div class="wf-source-placeholder">Monaco editor loading\u2026</div>';
    _pendingSource = { path, line };
    return;
  }
  _pendingSource = null;
  const m = (window as any).monaco;
  const lang = path.endsWith('.ipynb') ? 'json' : 'python';
  if (!_monacoEditor) {
    col.innerHTML = '<div id="tests-monaco-container" style="width:100%;height:100%;"></div>';
    _monacoEditor = m.editor.create(document.getElementById('tests-monaco-container'), {
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

/** Create a group header with collapse support. */
function _makeGroupHeader(label: string, groupKey: string | null, groupItems: any[], collapsed = false): HTMLElement {
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

  if (groupKey) {
    header.addEventListener('click', () => {
      if (_collapsedGroups.has(groupKey)) _collapsedGroups.delete(groupKey);
      else _collapsedGroups.add(groupKey);
      _allCollapsed = _collapsedGroups.size > 0 && _collapsedGroups.size >= _countVisibleGroups();
      updateList();
    });
  }

  return header;
}

function _countVisibleGroups(): number {
  const filtered = applyFilters(_tests);
  if (_groupBy === 'module') {
    return new Set(filtered.map(t => t.module || '')).size;
  } else if (_groupBy === 'tier') {
    return new Set(filtered.map(t => tier(t))).size;
  }
  return 0;
}

function _toggleCollapseAll(): void {
  _allCollapsed = !_allCollapsed;
  if (_allCollapsed) {
    const filtered = applyFilters(_tests);
    if (_groupBy === 'module') {
      for (const t of filtered) _collapsedGroups.add(t.module || '');
    } else if (_groupBy === 'tier') {
      for (const t of filtered) _collapsedGroups.add(tier(t));
    }
  } else {
    _collapsedGroups.clear();
  }
  updateList();
}

function _syncCollapseBtn(): void {
  const btn = document.getElementById('tests-collapse-btn');
  if (!btn) return;
  btn.title = _allCollapsed ? 'Expand All' : 'Collapse All';
  btn.style.transform = _allCollapsed ? 'rotate(0deg)' : 'rotate(180deg)';
}

function modDisplayName(path: string): string {
  if (!path) return '(unknown)';
  const p = path.replace(/\\/g, '/');
  const parts = p.split('/');
  const file = parts[parts.length - 1].replace(/\.py$/, '');
  const parent = parts.length > 1 ? parts[parts.length - 2] : '';
  return parent ? `${parent}/${file}` : file;
}

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
