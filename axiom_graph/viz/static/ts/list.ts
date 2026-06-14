// =============================================================================
// list.ts -- Node list table renderer
//   Grouping (flat + nested tree), verify, tooltips, column toggle.
//   Source panel, diff viewer, doc preview, breadcrumb navigation, and
//   Monaco init are in list-source-panel.ts.
// =============================================================================

import type { AxiomNode, StalenessMap } from './types.js';
import { displayStaleness } from './types.js';
import { esc, apiFetch } from './view-utils.js';
import { NODE_TYPE_COLORS, NODE_TYPE_DEFAULT } from './graph.js';
import {
  openSource, openDocPreview, openMarkdownPreview,
  openSourceWithDiff, toggleFocus, toggleDiff, closeSource, viewInGraph,
  initMonaco, vscodeUri,
  setSourcePanelCallbacks, bindSourcePanelHandlers, getDocCurrentId,
} from './list-source-panel.js';

// Re-export source panel functions for consumers that import from list.ts
export { openSource, openSourceWithDiff, toggleFocus, toggleDiff, closeSource, viewInGraph, initMonaco, getDocCurrentId };

// Callbacks to avoid circular imports
let selectNodeFn: (nodeId: string) => void = () => {};
let setViewFn: (view: string) => void = () => {};
let setStatusFn: (msg: string) => void = () => {};
let getAppState: () => any = () => ({});
let colResizeWireFn: ((handle: HTMLElement) => void) | null = null;

export function setListCallbacks(callbacks: {
  selectNode: (nodeId: string) => void;
  setView: (view: string) => void;
  setStatus: (msg: string) => void;
  getAppState: () => any;
  renderStalenessSummary?: (map: StalenessMap) => void;
  colResizeWire?: (handle: HTMLElement) => void;
}): void {
  selectNodeFn = callbacks.selectNode;
  setViewFn = callbacks.setView;
  setStatusFn = callbacks.setStatus;
  getAppState = callbacks.getAppState;
  if (callbacks.renderStalenessSummary) _renderStalenessSummaryFn = callbacks.renderStalenessSummary;
  if (callbacks.colResizeWire) colResizeWireFn = callbacks.colResizeWire;
  // Wire source panel with same callbacks + list state accessors
  setSourcePanelCallbacks({
    selectNode: callbacks.selectNode,
    setView: callbacks.setView,
    setStatus: callbacks.setStatus,
    getAppState: callbacks.getAppState,
    getNodes: () => _nodes,
    getSelectedIds: () => new Set(_selectedIds),
  });
}

let _renderStalenessSummaryFn: ((map: StalenessMap) => void) | null = null;

// ── State ───────────────────────────────────────────────────────────────────

let _sortCol = 'title';
let _sortDir = 1;
let _nodes: AxiomNode[] = [];
let _stalenessMap: StalenessMap = {};
let _changeKinds: Record<string, string[]> = {};
let _groupBy: string | null = null;
let _selectedIds = new Set<string>();
let _collapsedGroups = new Set<string>();
let _allCollapsed = false;
let _causeCache: Record<string, any> = {};
let _columns: Record<string, boolean> = { check: true, title: true, node_type: true, location: true, staleness: true, tags: true };

// ── Public API ──────────────────────────────────────────────────────────────

/** Set the net change-kinds map (node_id -> [kind,...]) from the since response.
 *  Drives the per-node change-kind badge in the staleness cell. */
export function setChangeKinds(kinds: Record<string, string[]> | undefined): void {
  _changeKinds = kinds || {};
}

export function render(nodes: AxiomNode[], stalenessMap: StalenessMap): void {
  _nodes = nodes;
  _stalenessMap = stalenessMap || {};
  const idSet = new Set(nodes.map(n => n.id));
  for (const id of _selectedIds) {
    if (!idSet.has(id)) _selectedIds.delete(id);
  }
  _updateToolbar();
  _renderRows();
}

export function setGroupBy(field: string | null): void {
  _groupBy = field || null;
  _collapsedGroups.clear();
  _allCollapsed = true;
  _renderRows();
}

export function getSelectedIds(): Set<string> { return new Set(_selectedIds); }

export function clearSelection(): void {
  _selectedIds.clear();
  _updateToolbar();
  _renderRows();
}

export function toggleColumn(colKey: string, visible: boolean): void {
  _columns[colKey] = visible;
  document.querySelectorAll('#node-table th[data-colkey]').forEach(th => {
    (th as HTMLElement).classList.toggle('hidden', !_columns[(th as HTMLElement).dataset.colkey || '']);
  });
  document.querySelectorAll('#col-menu input[data-colkey]').forEach(cb => {
    (cb as HTMLInputElement).checked = !!_columns[(cb as HTMLElement).dataset.colkey || ''];
  });
  _renderRows();
}

export function toggleCollapseAll(): void {
  if (!_groupBy) return;
  _allCollapsed = !_allCollapsed;
  if (_allCollapsed) {
    const groups = _groupBy === 'location'
      ? _buildTree(_nodes)
      : _groupNodes(_nodes);
    const keys = _collectGroupKeys(groups);
    _collapsedGroups = new Set(keys);
  } else {
    _collapsedGroups.clear();
  }
  const btn = document.getElementById('btn-collapse-all');
  if (btn) btn.textContent = _allCollapsed ? 'Expand All' : 'Collapse All';
  _renderRows();
}

export function clearCauseCache(): void {
  for (const k of Object.keys(_causeCache)) delete _causeCache[k];
}

// ── Toolbar ─────────────────────────────────────────────────────────────────

function _updateToolbar(): void {
  const countEl = document.getElementById('list-count');
  const sel = _selectedIds.size;
  if (countEl) {
    countEl.textContent = sel > 0
      ? `${sel} of ${_nodes.length} selected`
      : `${_nodes.length} nodes`;
  }
  const verifyBtn = document.getElementById('btn-verify-selected') as HTMLButtonElement | null;
  if (verifyBtn) {
    verifyBtn.disabled = sel === 0;
    verifyBtn.textContent = sel > 0 ? `\u2713 Verify (${sel})` : '\u2713 Verify Selected';
  }
}

// ── Grouping ────────────────────────────────────────────────────────────────

interface GroupEntry {
  key: string | null;
  label: string | null;
  nodes: AxiomNode[];
  depth?: number;
  isDir?: boolean;
  nodeCount?: number;
  children?: GroupEntry[] | null;
}

function _collectGroupKeys(groups: GroupEntry[]): string[] {
  const keys: string[] = [];
  for (const g of groups) {
    if (g.key !== null) keys.push(g.key);
    if (g.children) keys.push(..._collectGroupKeys(g.children));
  }
  return keys;
}

function _groupNodes(nodes: AxiomNode[]): GroupEntry[] {
  if (!_groupBy) return [{ key: null, label: null, nodes }];
  const groups: Record<string, AxiomNode[]> = {};
  for (const node of nodes) {
    let key: string;
    switch (_groupBy) {
      case 'node_type': key = node.node_type || '(unknown)'; break;
      case 'subtype': key = node.subtype || (node.node_type === 'composite_process' ? 'module' : 'function'); break;
      case 'staleness': key = displayStaleness(_stalenessMap[node.id]); break;
      case 'tag': key = (node.tags || [])[0] || '(untagged)'; break;
      default: key = '(all)';
    }
    if (!groups[key]) groups[key] = [];
    groups[key].push(node);
  }
  return Object.entries(groups)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([key, nodes]) => ({ key, label: key, nodes, children: null }));
}

function _buildTree(nodes: AxiomNode[]): GroupEntry[] {
  const tree: any = {};
  const ensure = (obj: any, parts: string[]) => {
    let cur = obj;
    for (const p of parts) {
      if (!cur.__dirs) cur.__dirs = {};
      if (!cur.__dirs[p]) cur.__dirs[p] = { __nodes: [], __dirs: {} };
      cur = cur.__dirs[p];
    }
    return cur;
  };

  for (const node of nodes) {
    const loc = (node.location || '').replace(/#L\d+(-L\d+)?$/, '');
    const parts = loc.split('/').filter(Boolean);
    if (parts.length === 0) {
      if (!tree.__nodes) tree.__nodes = [];
      tree.__nodes.push(node);
      continue;
    }
    const fileName = parts.pop()!;
    const dirNode = parts.length > 0 ? ensure(tree, parts) : tree;
    if (!dirNode.__nodes) dirNode.__nodes = [];
    if (!dirNode.__files) dirNode.__files = {};
    if (!dirNode.__files[fileName]) dirNode.__files[fileName] = [];
    dirNode.__files[fileName].push(node);
  }

  return _treeToGroups(tree, '', 0);
}

function _treeToGroups(node: any, prefix: string, depth: number): GroupEntry[] {
  const results: GroupEntry[] = [];
  if (!node) return results;

  const dirs = Object.keys(node.__dirs || {}).sort();
  const files = Object.keys(node.__files || {}).sort();

  for (const dir of dirs) {
    const path = prefix ? `${prefix}/${dir}` : dir;
    const allNodes = _countTreeNodes(node.__dirs[dir]);
    results.push({
      key: `dir:${path}`,
      label: `${dir}/`,
      nodes: [],
      depth,
      isDir: true,
      nodeCount: allNodes,
      children: _treeToGroups(node.__dirs[dir], path, depth + 1),
    });
  }

  for (const file of files) {
    const path = prefix ? `${prefix}/${file}` : file;
    results.push({
      key: `file:${path}`,
      label: file,
      nodes: node.__files[file],
      depth,
      isDir: false,
      nodeCount: node.__files[file].length,
      children: null,
    });
  }

  if (node.__nodes && node.__nodes.length) {
    results.push({ key: null, label: null, nodes: node.__nodes, depth, children: null });
  }

  return results;
}

function _countTreeNodes(node: any): number {
  let count = 0;
  for (const f of Object.values(node.__files || {}) as AxiomNode[][]) count += f.length;
  for (const d of Object.values(node.__dirs || {})) count += _countTreeNodes(d);
  if (node.__nodes) count += node.__nodes.length;
  return count;
}

function _visibleColCount(): number {
  let n = 0;
  for (const k of Object.keys(_columns)) if (_columns[k]) n++;
  return n;
}

// ── Row rendering ───────────────────────────────────────────────────────────

function _renderRows(): void {
  const sorted = [..._nodes].sort((a: any, b: any) => {
    let va = a[_sortCol] ?? '';
    let vb = b[_sortCol] ?? '';
    if (_sortCol === 'staleness') {
      va = displayStaleness(_stalenessMap[a.id]);
      vb = displayStaleness(_stalenessMap[b.id]);
    }
    if (typeof va === 'string') va = va.toLowerCase();
    if (typeof vb === 'string') vb = vb.toLowerCase();
    return _sortDir * (va < vb ? -1 : va > vb ? 1 : 0);
  });

  const tbody = document.getElementById('node-table-body');
  if (!tbody) return;
  tbody.innerHTML = '';
  const colSpan = _visibleColCount();

  if (_groupBy === 'location') {
    const tree = _buildTree(sorted);
    // Auto-populate collapsed set when starting collapsed
    if (_allCollapsed && _collapsedGroups.size === 0) {
      const keys = _collectGroupKeys(tree);
      for (const k of keys) _collapsedGroups.add(k);
    }
    _renderTreeGroups(tbody, tree, colSpan);
  } else {
    const groups = _groupNodes(sorted);
    // Auto-populate collapsed set when starting collapsed
    if (_allCollapsed && _collapsedGroups.size === 0) {
      const keys = _collectGroupKeys(groups);
      for (const k of keys) _collapsedGroups.add(k);
    }
    _renderFlatGroups(tbody, groups, colSpan);
  }

  const collapseBtn = document.getElementById('btn-collapse-all');
  if (collapseBtn) collapseBtn.textContent = _allCollapsed ? 'Expand All' : 'Collapse All';

  document.querySelectorAll('#node-table th.sortable').forEach(th => {
    th.classList.remove('sort-asc', 'sort-desc');
    if ((th as HTMLElement).dataset.col === _sortCol) {
      th.classList.add(_sortDir === 1 ? 'sort-asc' : 'sort-desc');
    }
  });
  _updateSelectAll();
}

function _renderFlatGroups(tbody: HTMLElement, groups: GroupEntry[], colSpan: number): void {
  for (const group of groups) {
    if (group.key !== null) {
      const collapsed = _collapsedGroups.has(group.key);
      const hdr = _makeGroupHeader(group.key, group.label || '', group.nodes.length, collapsed, colSpan, 0);
      tbody.appendChild(hdr);
      if (collapsed) continue;
    }
    const depth = group.key !== null ? 1 : 0;
    for (const node of group.nodes) tbody.appendChild(_makeNodeRow(node, depth));
  }
}

function _renderTreeGroups(tbody: HTMLElement, groups: GroupEntry[], colSpan: number): void {
  for (const group of groups) {
    if (group.key === null) {
      for (const node of group.nodes) tbody.appendChild(_makeNodeRow(node, 0));
      continue;
    }
    const collapsed = _collapsedGroups.has(group.key);
    const hdr = _makeGroupHeader(group.key, group.label || '', group.nodeCount || 0, collapsed, colSpan, group.depth || 0, group.isDir);
    tbody.appendChild(hdr);
    if (collapsed) continue;
    if (group.children) _renderTreeGroups(tbody, group.children, colSpan);
    if (group.nodes) {
      const nodeDepth = (group.depth || 0) + 1;
      for (const node of group.nodes) tbody.appendChild(_makeNodeRow(node, nodeDepth));
    }
  }
}

function _makeGroupHeader(key: string, label: string, count: number, collapsed: boolean, colSpan: number, depth = 0, isDir = false): HTMLTableRowElement {
  const tr = document.createElement('tr');
  tr.className = 'group-header' + (isDir ? ' group-dir' : '');
  const indent = 16 + depth * 20;
  tr.innerHTML =
    `<td colspan="${colSpan}" style="padding-left:${indent}px">` +
    `<span class="group-toggle">${collapsed ? '\u25b8' : '\u25be'}</span>` +
    `<span class="group-label">${esc(label)}</span>` +
    `<span class="group-count">${count}</span>` +
    `</td>`;
  tr.addEventListener('click', () => {
    if (_collapsedGroups.has(key)) _collapsedGroups.delete(key);
    else _collapsedGroups.add(key);
    _renderRows();
  });
  return tr;
}

function _makeNodeRow(node: AxiomNode, depth = 0): HTMLTableRowElement {
  const isGhost    = !!(node as any)._deleted;
  const staleness  = isGhost ? 'DELETED' : displayStaleness(_stalenessMap[node.id]);
  const typeColor  = NODE_TYPE_COLORS[node.node_type] || NODE_TYPE_DEFAULT;
  const tagChips   = (node.tags || []).slice(0, 5)
    .map(t => `<span class="tag-chip">${esc(t)}</span>`).join('');
  const isSelected = _selectedIds.has(node.id);
  const state = getAppState();
  const isActive   = state.selectedNodeId === node.id;
  const isMarkdownConfig = node.subtype === 'config' && node.location && node.location.endsWith('.md');
  const isCode     = node.subtype !== 'docjson' && node.node_type !== 'entity' && !isMarkdownConfig;
  const indent     = 16 + depth * 20;
  const firstCellIndent = depth > 0;

  const tr = document.createElement('tr');
  if (isActive) tr.classList.add('selected');
  if (isGhost) tr.classList.add('ghost-node');

  // Checkbox
  if (_columns.check) {
    const td = document.createElement('td');
    td.className = 'check-cell';
    if (firstCellIndent) td.style.paddingLeft = indent + 'px';
    if (!isGhost) {
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = isSelected;
      cb.addEventListener('change', e => {
        e.stopPropagation();
        if ((e.target as HTMLInputElement).checked) _selectedIds.add(node.id);
        else _selectedIds.delete(node.id);
        _updateToolbar();
        _updateSelectAll();
      });
      cb.addEventListener('click', e => e.stopPropagation());
      td.appendChild(cb);
    }
    tr.appendChild(td);
  }

  // Name + details button
  if (_columns.title) {
    const td = document.createElement('td');
    if (firstCellIndent && !_columns.check) td.style.paddingLeft = indent + 'px';
    const nameSpan = document.createElement('span');
    nameSpan.style.fontWeight = '500';
    nameSpan.textContent = node.title || node.id.split('::').pop() || '';
    td.appendChild(nameSpan);

    {
      const detBtn = document.createElement('button');
      detBtn.className = 'inline-details-btn';
      detBtn.textContent = '\u00b7\u00b7\u00b7';
      // Deleted ghosts are now selectable: their detail button opens an
      // old-side-only baseline-source view (new side is gone).
      detBtn.title = isGhost ? 'Show deleted baseline source' : 'Open detail panel';
      detBtn.addEventListener('click', e => {
        e.stopPropagation();
        if (isGhost) _showGhostSource(node);
        else selectNodeFn(node.id);
      });
      td.appendChild(detBtn);
    }
    tr.appendChild(td);
  }

  // Type
  if (_columns.node_type) {
    const td = document.createElement('td');
    td.innerHTML = `<span class="type-badge" style="background:${typeColor}">${esc(node.node_type)}</span>`;
    tr.appendChild(td);
  }

  // Location
  if (_columns.location) {
    const td = document.createElement('td');
    const loc = node.location || '';
    if (loc) {
      if (isGhost) {
        td.textContent = loc;
        td.style.color = '#636c76';
      } else {
        const link = document.createElement('a');
        link.className = 'loc-link';
        link.href = _vscodeUri(loc);
        link.textContent = loc;
        link.title = 'Open in editor';
        link.addEventListener('click', e => e.stopPropagation());
        td.appendChild(link);
      }
    }
    tr.appendChild(td);
  }

  // Staleness + verify
  if (_columns.staleness) {
    const td = document.createElement('td');
    td.className = 'staleness-cell';
    const badge = document.createElement('span');
    badge.className = `staleness-badge ${staleness}`;
    if (isGhost) {
      badge.textContent = 'deleted';
    } else {
      badge.textContent = staleness.replace(/_/g, ' ').toLowerCase();
      const isStale = staleness !== 'VERIFIED' && staleness !== 'VERIFIED' && staleness !== 'unknown';
      if (isStale) {
        badge.classList.add('clickable');
        badge.title = 'Click for staleness cause';
        badge.addEventListener('click', e => { e.stopPropagation(); _showStalenessCause(node.id, e.target as HTMLElement); });
      }
      td.appendChild(badge);
      if (isStale) {
        const vBtn = document.createElement('button');
        vBtn.className = 'row-verify-btn';
        vBtn.title = 'Mark as verified';
        vBtn.innerHTML = '&#x2713;';
        vBtn.addEventListener('click', e => { e.stopPropagation(); _verifyNode(node.id, vBtn); });
        td.appendChild(vBtn);
      }
    }
    if (isGhost) td.appendChild(badge);

    // Net change-kind badge(s) — added / content / desc / content+desc /
    // renamed / deleted. Reuses the detail-tab history-badge-* styling;
    // `added` maps to history-badge-initial (the existing initial/add class).
    const kinds = _changeKinds[node.id];
    if (kinds && kinds.length) {
      for (const kind of kinds) {
        const kb = document.createElement('span');
        const cls = kind === 'added' ? 'history-badge-initial'
          : `history-badge-${kind.replace('+', '-')}`;
        kb.className = `history-badge ${cls} change-kind-badge`;
        kb.textContent = kind;
        kb.title = `Net change since baseline: ${kind}`;
        td.appendChild(kb);
      }
    }
    tr.appendChild(td);
  }

  // Tags
  if (_columns.tags) {
    const td = document.createElement('td');
    td.innerHTML = tagChips;
    tr.appendChild(td);
  }

  // Row click
  if (isGhost) {
    // Deleted ghosts are selectable -> old-side-only baseline source view.
    tr.addEventListener('click', () => _showGhostSource(node));
  } else {
    tr.addEventListener('click', () => {
      if (isCode && node.location) {
        openSource(node.id);
      } else if (node.subtype === 'docjson') {
        openDocPreview(node.id);
      } else if (isMarkdownConfig) {
        openMarkdownPreview(node.id);
      } else {
        selectNodeFn(node.id);
      }
    });
  }
  return tr;
}

/** Render the git-recovered baseline source of a deleted ghost (old side only).
 *
 *  A deleted node is purged from the index, so it has no live two-sided diff.
 *  This surfaces the `recovered_source` carried on the ghost dict (the since
 *  endpoint recovered it via git) in a single read-only pane. When recovery
 *  failed (unreachable blob) a clear message is shown rather than an empty box.
 */
function _showGhostSource(node: AxiomNode): void {
  const src = node.recovered_source;
  const title = node.title || node.id.split('::').pop() || node.id;
  const loc = node.location || '';
  const existing = document.getElementById('ghost-source-overlay');
  if (existing) existing.remove();
  const overlay = document.createElement('div');
  overlay.id = 'ghost-source-overlay';
  overlay.className = 'ghost-source-overlay';
  const panel = document.createElement('div');
  panel.className = 'ghost-source-panel';
  const header = document.createElement('div');
  header.className = 'ghost-source-header';
  header.innerHTML =
    `<span class="ghost-source-title">Deleted: <strong>${esc(title)}</strong>` +
    `<span class="history-badge history-badge-deleted change-kind-badge">deleted</span></span>` +
    `<span class="ghost-source-loc">${esc(loc)} (baseline — new side gone)</span>`;
  const close = document.createElement('button');
  close.className = 'ghost-source-close';
  close.textContent = '×';
  close.title = 'Close';
  close.addEventListener('click', () => overlay.remove());
  header.appendChild(close);
  const body = document.createElement('pre');
  body.className = 'ghost-source-body';
  if (src && src.trim()) {
    body.textContent = src;
  } else {
    body.textContent = 'Baseline source could not be recovered from git for this deleted node.';
    body.classList.add('ghost-source-empty');
  }
  panel.appendChild(header);
  panel.appendChild(body);
  overlay.appendChild(panel);
  overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
  document.body.appendChild(overlay);
}

function _updateSelectAll(): void {
  const el = document.getElementById('select-all-check') as HTMLInputElement | null;
  if (!el) return;
  const total = _nodes.length;
  const sel = _selectedIds.size;
  el.checked = total > 0 && sel === total;
  el.indeterminate = sel > 0 && sel < total;
}

// ── VSCode URI (delegated to source panel) ──────────────────────────────────

function _vscodeUri(location: string): string {
  return vscodeUri(location);
}

async function _showStalenessCause(nodeId: string, target: HTMLElement): Promise<void> {
  _closeTooltip();
  if (!_causeCache[nodeId]) {
    try { _causeCache[nodeId] = await apiFetch(`/api/nodes/${encodeURIComponent(nodeId)}/staleness-cause`); }
    catch { return; }
  }
  const cause = _causeCache[nodeId];
  const tip = document.createElement('div');
  tip.className = 'staleness-tooltip';
  tip.id = 'staleness-tooltip-active';
  let html = `<div class="tooltip-header staleness-badge ${cause.status || ''}">${esc(cause.status || 'Unknown')}</div>`;
  if (cause.cause) html += `<div class="tooltip-body">${esc(cause.cause)}</div>`;
  if (cause.details && cause.details.stale_linked_nodes) {
    html += `<div class="tooltip-linked"><strong>Linked:</strong> ${cause.details.stale_linked_nodes.map((n: string) => esc(n)).join(', ')}</div>`;
  }
  if (cause.verification) html += `<div class="tooltip-verified">Verified: ${cause.verification.verified_at || 'never'} by ${cause.verification.verified_by || '?'}</div>`;
  tip.innerHTML = html;
  const rect = target.getBoundingClientRect();
  tip.style.position = 'fixed';
  tip.style.top = (rect.bottom + 4) + 'px';
  tip.style.left = Math.min(rect.left, window.innerWidth - 320) + 'px';
  document.body.appendChild(tip);
  setTimeout(() => {
    const handler = () => { _closeTooltip(); document.removeEventListener('click', handler); };
    document.addEventListener('click', handler);
  }, 10);
}

function _closeTooltip(): void {
  const tip = document.getElementById('staleness-tooltip-active');
  if (tip) tip.remove();
}

async function _verifyNode(nodeId: string, btn: HTMLButtonElement): Promise<void> {
  const reason = prompt('Verification reason (optional):');
  if (reason === null) return;
  btn.disabled = true; btn.textContent = '\u2026';
  try {
    await fetch(`/api/nodes/${encodeURIComponent(nodeId)}/verify`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason: reason || null, verified_by: 'human' }),
    });
    const state = getAppState();
    state.stalenessMap[nodeId] = { own_status: 'VERIFIED', link_status: 'VERIFIED' };
    _causeCache = {};
    _renderRows();
  } catch (err) { console.error('Verify failed:', err); btn.disabled = false; btn.textContent = '\u2713'; }
}

export async function bulkVerify(): Promise<void> {
  const ids = [..._selectedIds];
  if (ids.length === 0) return;
  const reason = prompt(`Verify ${ids.length} node(s) -- reason (optional):`);
  if (reason === null) return;
  const btn = document.getElementById('btn-verify-selected') as HTMLButtonElement | null;
  if (btn) { btn.disabled = true; btn.textContent = '\u2713 Verifying\u2026'; }
  try {
    const res = await fetch('/api/nodes/bulk-verify', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ node_ids: ids, reason: reason || null, verified_by: 'human' }),
    });
    const data = await res.json();
    let ok = 0;
    const state = getAppState();
    for (const r of data.results) { if (r.ok) { state.stalenessMap[r.node_id] = { own_status: 'VERIFIED', link_status: 'VERIFIED' }; ok++; } }
    _selectedIds.clear();
    _causeCache = {};
    _updateToolbar();
    _renderRows();
    if (_renderStalenessSummaryFn) _renderStalenessSummaryFn(state.stalenessMap);
    setStatusFn(`Verified ${ok} of ${ids.length} nodes`);
  } catch (err: any) {
    console.error('Bulk verify failed:', err);
    setStatusFn(`Bulk verify failed: ${err.message}`);
    if (btn) { btn.disabled = false; btn.textContent = '\u2713 Verify Selected'; }
  }
}

// ── Sort and event binding (called once on DOMContentLoaded) ────────────────

export function bindSortHandlers(): void {
  document.querySelectorAll('#node-table th.sortable').forEach(th => {
    th.addEventListener('click', () => {
      if (_sortCol === (th as HTMLElement).dataset.col) _sortDir *= -1;
      else { _sortCol = (th as HTMLElement).dataset.col || 'title'; _sortDir = 1; }
      _renderRows();
    });
  });

  const selectAll = document.getElementById('select-all-check') as HTMLInputElement | null;
  if (selectAll) {
    selectAll.addEventListener('change', e => {
      if ((e.target as HTMLInputElement).checked) { for (const n of _nodes) _selectedIds.add(n.id); }
      else _selectedIds.clear();
      _updateToolbar();
      _renderRows();
    });
  }

  const colMenuBtn = document.getElementById('btn-col-menu');
  const colMenu = document.getElementById('col-menu');
  if (colMenuBtn && colMenu) {
    colMenuBtn.addEventListener('click', e => { e.stopPropagation(); colMenu.classList.toggle('hidden'); });
    document.addEventListener('click', e => { if (!(e.target as HTMLElement).closest('.col-menu-wrap')) colMenu.classList.add('hidden'); });
    colMenu.querySelectorAll('input[data-colkey]').forEach(cb => {
      cb.addEventListener('change', e => { e.stopPropagation(); toggleColumn((e.target as HTMLElement).dataset.colkey || '', (e.target as HTMLInputElement).checked); });
    });
  }

  const collapseBtn = document.getElementById('btn-collapse-all');
  if (collapseBtn) collapseBtn.addEventListener('click', () => toggleCollapseAll());

  // Delegate source panel handler binding
  bindSourcePanelHandlers();

  // Table <-> source column resize handle
  const tableResize = document.getElementById('list-table-resize');
  if (tableResize && colResizeWireFn) colResizeWireFn(tableResize);
}
