// =============================================================================
// list.ts -- Node list table renderer
//   Grouping (flat + nested tree), verify, tooltips, column toggle.
//   Source panel, diff viewer, doc preview, breadcrumb navigation, and
//   Monaco init are in list-source-panel.ts.
// =============================================================================
import { displayStaleness } from './types.js';
import { esc, apiFetch } from './view-utils.js';
import { NODE_TYPE_COLORS, NODE_TYPE_DEFAULT } from './graph.js';
import { openSource, openDocPreview, openMarkdownPreview, openSourceWithDiff, toggleFocus, toggleDiff, closeSource, viewInGraph, initMonaco, vscodeUri, setSourcePanelCallbacks, bindSourcePanelHandlers, getDocCurrentId, } from './list-source-panel.js';
// Re-export source panel functions for consumers that import from list.ts
export { openSource, openSourceWithDiff, toggleFocus, toggleDiff, closeSource, viewInGraph, initMonaco, getDocCurrentId };
// Callbacks to avoid circular imports
let selectNodeFn = () => { };
let setViewFn = () => { };
let setStatusFn = () => { };
let getAppState = () => ({});
let colResizeWireFn = null;
export function setListCallbacks(callbacks) {
    selectNodeFn = callbacks.selectNode;
    setViewFn = callbacks.setView;
    setStatusFn = callbacks.setStatus;
    getAppState = callbacks.getAppState;
    if (callbacks.renderStalenessSummary)
        _renderStalenessSummaryFn = callbacks.renderStalenessSummary;
    if (callbacks.colResizeWire)
        colResizeWireFn = callbacks.colResizeWire;
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
let _renderStalenessSummaryFn = null;
// ── State ───────────────────────────────────────────────────────────────────
let _sortCol = 'title';
let _sortDir = 1;
let _nodes = [];
let _stalenessMap = {};
let _groupBy = null;
let _selectedIds = new Set();
let _collapsedGroups = new Set();
let _allCollapsed = false;
let _causeCache = {};
let _columns = { check: true, title: true, node_type: true, location: true, staleness: true, tags: true };
// ── Public API ──────────────────────────────────────────────────────────────
export function render(nodes, stalenessMap) {
    _nodes = nodes;
    _stalenessMap = stalenessMap || {};
    const idSet = new Set(nodes.map(n => n.id));
    for (const id of _selectedIds) {
        if (!idSet.has(id))
            _selectedIds.delete(id);
    }
    _updateToolbar();
    _renderRows();
}
export function setGroupBy(field) {
    _groupBy = field || null;
    _collapsedGroups.clear();
    _allCollapsed = true;
    _renderRows();
}
export function getSelectedIds() { return new Set(_selectedIds); }
export function clearSelection() {
    _selectedIds.clear();
    _updateToolbar();
    _renderRows();
}
export function toggleColumn(colKey, visible) {
    _columns[colKey] = visible;
    document.querySelectorAll('#node-table th[data-colkey]').forEach(th => {
        th.classList.toggle('hidden', !_columns[th.dataset.colkey || '']);
    });
    document.querySelectorAll('#col-menu input[data-colkey]').forEach(cb => {
        cb.checked = !!_columns[cb.dataset.colkey || ''];
    });
    _renderRows();
}
export function toggleCollapseAll() {
    if (!_groupBy)
        return;
    _allCollapsed = !_allCollapsed;
    if (_allCollapsed) {
        const groups = _groupBy === 'location'
            ? _buildTree(_nodes)
            : _groupNodes(_nodes);
        const keys = _collectGroupKeys(groups);
        _collapsedGroups = new Set(keys);
    }
    else {
        _collapsedGroups.clear();
    }
    const btn = document.getElementById('btn-collapse-all');
    if (btn)
        btn.textContent = _allCollapsed ? 'Expand All' : 'Collapse All';
    _renderRows();
}
export function clearCauseCache() {
    for (const k of Object.keys(_causeCache))
        delete _causeCache[k];
}
// ── Toolbar ─────────────────────────────────────────────────────────────────
function _updateToolbar() {
    const countEl = document.getElementById('list-count');
    const sel = _selectedIds.size;
    if (countEl) {
        countEl.textContent = sel > 0
            ? `${sel} of ${_nodes.length} selected`
            : `${_nodes.length} nodes`;
    }
    const verifyBtn = document.getElementById('btn-verify-selected');
    if (verifyBtn) {
        verifyBtn.disabled = sel === 0;
        verifyBtn.textContent = sel > 0 ? `\u2713 Verify (${sel})` : '\u2713 Verify Selected';
    }
}
function _collectGroupKeys(groups) {
    const keys = [];
    for (const g of groups) {
        if (g.key !== null)
            keys.push(g.key);
        if (g.children)
            keys.push(..._collectGroupKeys(g.children));
    }
    return keys;
}
function _groupNodes(nodes) {
    if (!_groupBy)
        return [{ key: null, label: null, nodes }];
    const groups = {};
    for (const node of nodes) {
        let key;
        switch (_groupBy) {
            case 'node_type':
                key = node.node_type || '(unknown)';
                break;
            case 'subtype':
                key = node.subtype || (node.node_type === 'composite_process' ? 'module' : 'function');
                break;
            case 'staleness':
                key = displayStaleness(_stalenessMap[node.id]);
                break;
            case 'tag':
                key = (node.tags || [])[0] || '(untagged)';
                break;
            default: key = '(all)';
        }
        if (!groups[key])
            groups[key] = [];
        groups[key].push(node);
    }
    return Object.entries(groups)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([key, nodes]) => ({ key, label: key, nodes, children: null }));
}
function _buildTree(nodes) {
    const tree = {};
    const ensure = (obj, parts) => {
        let cur = obj;
        for (const p of parts) {
            if (!cur.__dirs)
                cur.__dirs = {};
            if (!cur.__dirs[p])
                cur.__dirs[p] = { __nodes: [], __dirs: {} };
            cur = cur.__dirs[p];
        }
        return cur;
    };
    for (const node of nodes) {
        const loc = (node.location || '').replace(/#L\d+(-L\d+)?$/, '');
        const parts = loc.split('/').filter(Boolean);
        if (parts.length === 0) {
            if (!tree.__nodes)
                tree.__nodes = [];
            tree.__nodes.push(node);
            continue;
        }
        const fileName = parts.pop();
        const dirNode = parts.length > 0 ? ensure(tree, parts) : tree;
        if (!dirNode.__nodes)
            dirNode.__nodes = [];
        if (!dirNode.__files)
            dirNode.__files = {};
        if (!dirNode.__files[fileName])
            dirNode.__files[fileName] = [];
        dirNode.__files[fileName].push(node);
    }
    return _treeToGroups(tree, '', 0);
}
function _treeToGroups(node, prefix, depth) {
    const results = [];
    if (!node)
        return results;
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
function _countTreeNodes(node) {
    let count = 0;
    for (const f of Object.values(node.__files || {}))
        count += f.length;
    for (const d of Object.values(node.__dirs || {}))
        count += _countTreeNodes(d);
    if (node.__nodes)
        count += node.__nodes.length;
    return count;
}
function _visibleColCount() {
    let n = 0;
    for (const k of Object.keys(_columns))
        if (_columns[k])
            n++;
    return n;
}
// ── Row rendering ───────────────────────────────────────────────────────────
function _renderRows() {
    const sorted = [..._nodes].sort((a, b) => {
        let va = a[_sortCol] ?? '';
        let vb = b[_sortCol] ?? '';
        if (_sortCol === 'staleness') {
            va = displayStaleness(_stalenessMap[a.id]);
            vb = displayStaleness(_stalenessMap[b.id]);
        }
        if (typeof va === 'string')
            va = va.toLowerCase();
        if (typeof vb === 'string')
            vb = vb.toLowerCase();
        return _sortDir * (va < vb ? -1 : va > vb ? 1 : 0);
    });
    const tbody = document.getElementById('node-table-body');
    if (!tbody)
        return;
    tbody.innerHTML = '';
    const colSpan = _visibleColCount();
    if (_groupBy === 'location') {
        const tree = _buildTree(sorted);
        // Auto-populate collapsed set when starting collapsed
        if (_allCollapsed && _collapsedGroups.size === 0) {
            const keys = _collectGroupKeys(tree);
            for (const k of keys)
                _collapsedGroups.add(k);
        }
        _renderTreeGroups(tbody, tree, colSpan);
    }
    else {
        const groups = _groupNodes(sorted);
        // Auto-populate collapsed set when starting collapsed
        if (_allCollapsed && _collapsedGroups.size === 0) {
            const keys = _collectGroupKeys(groups);
            for (const k of keys)
                _collapsedGroups.add(k);
        }
        _renderFlatGroups(tbody, groups, colSpan);
    }
    const collapseBtn = document.getElementById('btn-collapse-all');
    if (collapseBtn)
        collapseBtn.textContent = _allCollapsed ? 'Expand All' : 'Collapse All';
    document.querySelectorAll('#node-table th.sortable').forEach(th => {
        th.classList.remove('sort-asc', 'sort-desc');
        if (th.dataset.col === _sortCol) {
            th.classList.add(_sortDir === 1 ? 'sort-asc' : 'sort-desc');
        }
    });
    _updateSelectAll();
}
function _renderFlatGroups(tbody, groups, colSpan) {
    for (const group of groups) {
        if (group.key !== null) {
            const collapsed = _collapsedGroups.has(group.key);
            const hdr = _makeGroupHeader(group.key, group.label || '', group.nodes.length, collapsed, colSpan, 0);
            tbody.appendChild(hdr);
            if (collapsed)
                continue;
        }
        const depth = group.key !== null ? 1 : 0;
        for (const node of group.nodes)
            tbody.appendChild(_makeNodeRow(node, depth));
    }
}
function _renderTreeGroups(tbody, groups, colSpan) {
    for (const group of groups) {
        if (group.key === null) {
            for (const node of group.nodes)
                tbody.appendChild(_makeNodeRow(node, 0));
            continue;
        }
        const collapsed = _collapsedGroups.has(group.key);
        const hdr = _makeGroupHeader(group.key, group.label || '', group.nodeCount || 0, collapsed, colSpan, group.depth || 0, group.isDir);
        tbody.appendChild(hdr);
        if (collapsed)
            continue;
        if (group.children)
            _renderTreeGroups(tbody, group.children, colSpan);
        if (group.nodes) {
            const nodeDepth = (group.depth || 0) + 1;
            for (const node of group.nodes)
                tbody.appendChild(_makeNodeRow(node, nodeDepth));
        }
    }
}
function _makeGroupHeader(key, label, count, collapsed, colSpan, depth = 0, isDir = false) {
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
        if (_collapsedGroups.has(key))
            _collapsedGroups.delete(key);
        else
            _collapsedGroups.add(key);
        _renderRows();
    });
    return tr;
}
function _makeNodeRow(node, depth = 0) {
    const isGhost = !!node._deleted;
    const staleness = isGhost ? 'DELETED' : displayStaleness(_stalenessMap[node.id]);
    const typeColor = NODE_TYPE_COLORS[node.node_type] || NODE_TYPE_DEFAULT;
    const tagChips = (node.tags || []).slice(0, 5)
        .map(t => `<span class="tag-chip">${esc(t)}</span>`).join('');
    const isSelected = _selectedIds.has(node.id);
    const state = getAppState();
    const isActive = state.selectedNodeId === node.id;
    const isMarkdownConfig = node.subtype === 'config' && node.location && node.location.endsWith('.md');
    const isCode = node.subtype !== 'docjson' && node.node_type !== 'entity' && !isMarkdownConfig;
    const indent = 16 + depth * 20;
    const firstCellIndent = depth > 0;
    const tr = document.createElement('tr');
    if (isActive)
        tr.classList.add('selected');
    if (isGhost)
        tr.classList.add('ghost-node');
    // Checkbox
    if (_columns.check) {
        const td = document.createElement('td');
        td.className = 'check-cell';
        if (firstCellIndent)
            td.style.paddingLeft = indent + 'px';
        if (!isGhost) {
            const cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.checked = isSelected;
            cb.addEventListener('change', e => {
                e.stopPropagation();
                if (e.target.checked)
                    _selectedIds.add(node.id);
                else
                    _selectedIds.delete(node.id);
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
        if (firstCellIndent && !_columns.check)
            td.style.paddingLeft = indent + 'px';
        const nameSpan = document.createElement('span');
        nameSpan.style.fontWeight = '500';
        nameSpan.textContent = node.title || node.id.split('::').pop() || '';
        td.appendChild(nameSpan);
        if (!isGhost) {
            const detBtn = document.createElement('button');
            detBtn.className = 'inline-details-btn';
            detBtn.textContent = '\u00b7\u00b7\u00b7';
            detBtn.title = 'Open detail panel';
            detBtn.addEventListener('click', e => {
                e.stopPropagation();
                selectNodeFn(node.id);
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
            }
            else {
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
        }
        else {
            badge.textContent = staleness.replace(/_/g, ' ').toLowerCase();
            const isStale = staleness !== 'VERIFIED' && staleness !== 'VERIFIED' && staleness !== 'unknown';
            if (isStale) {
                badge.classList.add('clickable');
                badge.title = 'Click for staleness cause';
                badge.addEventListener('click', e => { e.stopPropagation(); _showStalenessCause(node.id, e.target); });
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
        if (isGhost)
            td.appendChild(badge);
        tr.appendChild(td);
    }
    // Tags
    if (_columns.tags) {
        const td = document.createElement('td');
        td.innerHTML = tagChips;
        tr.appendChild(td);
    }
    // Row click
    if (!isGhost) {
        tr.addEventListener('click', () => {
            if (isCode && node.location) {
                openSource(node.id);
            }
            else if (node.subtype === 'docjson') {
                openDocPreview(node.id);
            }
            else if (isMarkdownConfig) {
                openMarkdownPreview(node.id);
            }
            else {
                selectNodeFn(node.id);
            }
        });
    }
    return tr;
}
function _updateSelectAll() {
    const el = document.getElementById('select-all-check');
    if (!el)
        return;
    const total = _nodes.length;
    const sel = _selectedIds.size;
    el.checked = total > 0 && sel === total;
    el.indeterminate = sel > 0 && sel < total;
}
// ── VSCode URI (delegated to source panel) ──────────────────────────────────
function _vscodeUri(location) {
    return vscodeUri(location);
}
async function _showStalenessCause(nodeId, target) {
    _closeTooltip();
    if (!_causeCache[nodeId]) {
        try {
            _causeCache[nodeId] = await apiFetch(`/api/nodes/${encodeURIComponent(nodeId)}/staleness-cause`);
        }
        catch {
            return;
        }
    }
    const cause = _causeCache[nodeId];
    const tip = document.createElement('div');
    tip.className = 'staleness-tooltip';
    tip.id = 'staleness-tooltip-active';
    let html = `<div class="tooltip-header staleness-badge ${cause.status || ''}">${esc(cause.status || 'Unknown')}</div>`;
    if (cause.cause)
        html += `<div class="tooltip-body">${esc(cause.cause)}</div>`;
    if (cause.details && cause.details.stale_linked_nodes) {
        html += `<div class="tooltip-linked"><strong>Linked:</strong> ${cause.details.stale_linked_nodes.map((n) => esc(n)).join(', ')}</div>`;
    }
    if (cause.verification)
        html += `<div class="tooltip-verified">Verified: ${cause.verification.verified_at || 'never'} by ${cause.verification.verified_by || '?'}</div>`;
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
function _closeTooltip() {
    const tip = document.getElementById('staleness-tooltip-active');
    if (tip)
        tip.remove();
}
async function _verifyNode(nodeId, btn) {
    const reason = prompt('Verification reason (optional):');
    if (reason === null)
        return;
    btn.disabled = true;
    btn.textContent = '\u2026';
    try {
        await fetch(`/api/nodes/${encodeURIComponent(nodeId)}/verify`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ reason: reason || null, verified_by: 'human' }),
        });
        const state = getAppState();
        state.stalenessMap[nodeId] = { own_status: 'VERIFIED', link_status: 'VERIFIED' };
        _causeCache = {};
        _renderRows();
    }
    catch (err) {
        console.error('Verify failed:', err);
        btn.disabled = false;
        btn.textContent = '\u2713';
    }
}
export async function bulkVerify() {
    const ids = [..._selectedIds];
    if (ids.length === 0)
        return;
    const reason = prompt(`Verify ${ids.length} node(s) -- reason (optional):`);
    if (reason === null)
        return;
    const btn = document.getElementById('btn-verify-selected');
    if (btn) {
        btn.disabled = true;
        btn.textContent = '\u2713 Verifying\u2026';
    }
    try {
        const res = await fetch('/api/nodes/bulk-verify', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ node_ids: ids, reason: reason || null, verified_by: 'human' }),
        });
        const data = await res.json();
        let ok = 0;
        const state = getAppState();
        for (const r of data.results) {
            if (r.ok) {
                state.stalenessMap[r.node_id] = { own_status: 'VERIFIED', link_status: 'VERIFIED' };
                ok++;
            }
        }
        _selectedIds.clear();
        _causeCache = {};
        _updateToolbar();
        _renderRows();
        if (_renderStalenessSummaryFn)
            _renderStalenessSummaryFn(state.stalenessMap);
        setStatusFn(`Verified ${ok} of ${ids.length} nodes`);
    }
    catch (err) {
        console.error('Bulk verify failed:', err);
        setStatusFn(`Bulk verify failed: ${err.message}`);
        if (btn) {
            btn.disabled = false;
            btn.textContent = '\u2713 Verify Selected';
        }
    }
}
// ── Sort and event binding (called once on DOMContentLoaded) ────────────────
export function bindSortHandlers() {
    document.querySelectorAll('#node-table th.sortable').forEach(th => {
        th.addEventListener('click', () => {
            if (_sortCol === th.dataset.col)
                _sortDir *= -1;
            else {
                _sortCol = th.dataset.col || 'title';
                _sortDir = 1;
            }
            _renderRows();
        });
    });
    const selectAll = document.getElementById('select-all-check');
    if (selectAll) {
        selectAll.addEventListener('change', e => {
            if (e.target.checked) {
                for (const n of _nodes)
                    _selectedIds.add(n.id);
            }
            else
                _selectedIds.clear();
            _updateToolbar();
            _renderRows();
        });
    }
    const colMenuBtn = document.getElementById('btn-col-menu');
    const colMenu = document.getElementById('col-menu');
    if (colMenuBtn && colMenu) {
        colMenuBtn.addEventListener('click', e => { e.stopPropagation(); colMenu.classList.toggle('hidden'); });
        document.addEventListener('click', e => { if (!e.target.closest('.col-menu-wrap'))
            colMenu.classList.add('hidden'); });
        colMenu.querySelectorAll('input[data-colkey]').forEach(cb => {
            cb.addEventListener('change', e => { e.stopPropagation(); toggleColumn(e.target.dataset.colkey || '', e.target.checked); });
        });
    }
    const collapseBtn = document.getElementById('btn-collapse-all');
    if (collapseBtn)
        collapseBtn.addEventListener('click', () => toggleCollapseAll());
    // Delegate source panel handler binding
    bindSourcePanelHandlers();
    // Table <-> source column resize handle
    const tableResize = document.getElementById('list-table-resize');
    if (tableResize && colResizeWireFn)
        colResizeWireFn(tableResize);
}
