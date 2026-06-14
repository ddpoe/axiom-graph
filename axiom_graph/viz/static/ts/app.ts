// =============================================================================
// app.ts -- Main controller: state, init, orchestration, event wiring
// =============================================================================

import type { AppState, AxiomNode, StalenessMap, StalenessEntry, FilterState } from './types.js';
import { displayStaleness } from './types.js';
import { esc, escHtml, apiFetch } from './view-utils.js';
import * as Graph from './graph.js';
import * as Detail from './detail.js';
import * as List from './list.js';
import * as Workflow from './workflow.js';
import * as Tests from './tests.js';
import * as DocApi from './doc-api.js';
import * as DocTree from './doc-tree.js';
import * as DocEditor from './doc-editor.js';
import * as SinceModal from './since-modal.js';

// ── State ────────────────────────────────────────────────────────────────────

const state: AppState = {
  view: 'graph',
  meta: null,
  allNodes: [],
  allEdges: [],
  stalenessMap: {},
  verificationMap: {},
  filteredNodes: [],
  selectedNodeId: null,
  isLargeProject: false,
  filters: {
    types: new Set(),
    tags: new Set(),
    status: 'all',
    staleness: 'all',
    subtypes: new Set(),
    edgeTypes: {},
    hideOrphans: true,
    showPrivate: false,
    showTests: false,
  },
  tagSearch: '',
  depth: 1,
  layout: 'cose',
  stepEdgeLabels: null,
  dflowWorkflows: [],
  sinceFilter: null,
  _sinceBaselineSha: null,
  _sinceNodeIds: null,
  _sinceDeletedNodes: null,
  _sinceUntilTimestamp: null,
  searchMode: 'keyword',
  searchQuery: '',
  searchResultNodes: null,
};

let _initialized = false;
let _TAG_LIMIT = 8;
let _allTags: [string, number][] = [];

// ── Expose state for sub-modules ────────────────────────────────────────────

export function getState(): AppState { return state; }

// ── Init ────────────────────────────────────────────────────────────────────

export async function init(): Promise<void> {
  if (!_initialized) {
    _initialized = true;

    // Register callbacks on sub-modules to avoid circular imports
    Graph.setEventHandlers(selectNode, closeDetail);
    Detail.setDetailCallbacks(selectNode, List.openSourceWithDiff, (nid: string, status: string) => {
      state.stalenessMap[nid] = { own_status: status, link_status: 'VERIFIED' };
      _renderStalenessSummary(state.stalenessMap);
    }, navigateToNode, (nid: string) => {
      if (!state.allNodes) return null;
      const n = state.allNodes.find((node: any) => node.id === nid);
      return n ? { node_type: n.node_type, subtype: n.subtype } : null;
    });
    List.setListCallbacks({
      selectNode,
      setView,
      setStatus,
      getAppState: () => state,
      renderStalenessSummary: _renderStalenessSummary,
      colResizeWire: (handle: HTMLElement) => ColResize._wire(handle),
    });
    Workflow.setWorkflowCallbacks({ selectNode, setView });
    Workflow.setColResizeInit(ColResize.init);
    Tests.setTestsCallbacks({ selectNode, setView });
    Tests.setColResizeInit(ColResize.init);

    // Static event bindings (once)
    document.getElementById('btn-graph-view')!.addEventListener('click', () => setView('graph'));
    document.getElementById('btn-list-view')!.addEventListener('click', () => setView('list'));
    document.getElementById('btn-workflow-view')!.addEventListener('click', () => setView('workflow'));
    document.getElementById('btn-tests-view')!.addEventListener('click', () => setView('tests'));
    document.getElementById('btn-docs-view')!.addEventListener('click', () => setView('docs'));
    document.getElementById('btn-check')!.addEventListener('click', check);
    document.getElementById('btn-rescan')!.addEventListener('click', rescan);
    document.getElementById('detail-close')!.addEventListener('click', closeDetail);

    document.querySelectorAll('input[name="group-by"]').forEach(radio => {
      radio.addEventListener('change', e => {
        List.setGroupBy((e.target as HTMLInputElement).value || null);
      });
    });
    // Default to Module/File grouping (matches checked radio in HTML)
    List.setGroupBy('location');
    document.getElementById('btn-list-check')!.addEventListener('click', check);
    document.getElementById('btn-verify-selected')!.addEventListener('click', () => List.bulkVerify());
    document.getElementById('btn-view-in-graph')!.addEventListener('click', () => List.viewInGraph());

    document.getElementById('show-private-check')!.addEventListener('change', e => {
      state.filters.showPrivate = (e.target as HTMLInputElement).checked;
      _applyFiltersAndRefresh();
    });
    document.getElementById('show-tests-check')!.addEventListener('change', e => {
      state.filters.showTests = (e.target as HTMLInputElement).checked;
      _applyFiltersAndRefresh();
    });

    document.getElementById('depth-slider')!.addEventListener('input', e => {
      state.depth = parseInt((e.target as HTMLInputElement).value, 10);
      document.getElementById('depth-val')!.textContent = (e.target as HTMLInputElement).value;
    });

    document.getElementById('hide-orphans-check')!.addEventListener('change', _onHideOrphansChange);

    document.querySelectorAll('.layout-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.layout-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        state.layout = (btn as HTMLElement).dataset.layout || 'cose';
        Graph.setLayout(state.layout);
      });
    });

    // Search
    let searchTimer: ReturnType<typeof setTimeout>;
    document.getElementById('search-input')!.addEventListener('input', e => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => onSearch((e.target as HTMLInputElement).value), 300);
    });
    document.getElementById('search-btn')!.addEventListener('click', () => {
      const input = document.getElementById('search-input') as HTMLInputElement;
      onSearch(input.value);
    });
    _initSearchModeDropdown();

    // Since filter — presets and browse
    document.querySelectorAll('.since-preset').forEach(btn => {
      btn.addEventListener('click', () => {
        // Close modal if open (preset takes precedence)
        SinceModal.closeModal();
        applySinceFilter((btn as HTMLElement).dataset.since || '');
      });
    });
    document.getElementById('btn-since-browse')!.addEventListener('click', () => SinceModal.openCommitPicker());
    document.getElementById('btn-since-clear')!.addEventListener('click', () => clearSinceFilter());
    SinceModal.initSinceModal({
      onSelect: (selection) => {
        if (selection.mode === 'single') {
          applySinceFilter('custom', selection.sha, { commitSubject: selection.commitSubject });
        } else {
          applySinceRange(selection.sinceSha, selection.untilSha, selection.sinceSubject, selection.untilSubject);
        }
      },
    });

    _initProjectSwitcher();

    // Init list sort handlers
    List.bindSortHandlers();

    // Init Monaco for all modules
    List.initMonaco();
    Workflow.initMonaco();
    Tests.initMonaco();
  }

  await _loadProject();
}

// ── Project data loader ─────────────────────────────────────────────────────

async function _loadProject(): Promise<void> {
  DocApi.reset();
  DocEditor.resetEditor();
  DocTree.resetTree();
  Tests.reset();
  Workflow.reset();
  clearSinceFilter();

  setStatus('Loading\u2026');

  // Dim sidebar during load to avoid blank-then-populate flash
  const sidebar = document.getElementById('sidebar');
  if (sidebar) sidebar.classList.add('loading');

  try {
    const meta = await apiFetch('/api/meta');
    state.meta = meta;
    document.getElementById('project-name')!.textContent = meta.project_id;
    document.title = `${meta.project_id} \u2014 Cortex`;
    _populateSidebarFilters(meta);
    _renderSidebarStats(meta);
    _populateStalenessFilter();
    _updateSearchModeDropdown();

    const data = await apiFetch('/api/all');
    state.allNodes = data.nodes;
    state.allEdges = data.edges;
    state.stalenessMap = data.staleness || {};
    state.verificationMap = data.verifications || {};
    state.isLargeProject = data.large || false;
    _populateSubtypeFilters(data.nodes);
    _renderStalenessSummary(state.stalenessMap);
    _buildTagFilterPanel(meta.tags);

    state.filters.edgeTypes = {};
    for (const et of meta.edge_types) state.filters.edgeTypes[et] = true;
    _populateEdgeTypeFilters(meta.edge_types);

    state.selectedNodeId = null;
    state.stepEdgeLabels = null;
    _applyFilters();

    if (state.isLargeProject) {
      _showGraphNotice(
        `Large project \u2014 ${meta.node_count} nodes detected. Using list view by default.`,
        () => _loadFullGraph(),
      );
      setView('list');
    } else {
      Graph.init(document.getElementById('cy')!);
      Graph.loadAll(state.filteredNodes, state.allEdges, state.stalenessMap, state.layout, state.stepEdgeLabels);
      const savedView = sessionStorage.getItem('cortex-view');
      if (savedView && ['graph', 'list', 'workflow', 'tests', 'docs'].includes(savedView)) {
        setView(savedView);
      } else {
        setView('graph');
      }
    }

    setStatus(`${meta.node_count} nodes \u00b7 ${meta.edge_count} edges`);
    _loadDflowData();
  } catch (err: any) {
    setStatus(`Error: ${err.message}`);
    console.error('Cortex Viz init error:', err);
  } finally {
    if (sidebar) sidebar.classList.remove('loading');
  }
}

// ── Status bar ──────────────────────────────────────────────────────────────

export function setStatus(msg: string): void {
  const el = document.getElementById('status-bar');
  if (el) el.textContent = msg;
}

// ── View switching ──────────────────────────────────────────────────────────

export function setView(view: string): void {
  state.view = view;
  sessionStorage.setItem('cortex-view', view);
  document.getElementById('btn-graph-view')!.classList.toggle('active', view === 'graph');
  document.getElementById('btn-list-view')!.classList.toggle('active', view === 'list');
  document.getElementById('btn-workflow-view')!.classList.toggle('active', view === 'workflow');
  document.getElementById('btn-tests-view')!.classList.toggle('active', view === 'tests');
  document.getElementById('btn-docs-view')!.classList.toggle('active', view === 'docs');
  document.getElementById('graph-view')!.classList.toggle('hidden', view !== 'graph');
  document.getElementById('list-view')!.classList.toggle('hidden', view !== 'list');
  document.getElementById('workflow-view')!.classList.toggle('hidden', view !== 'workflow');
  document.getElementById('tests-view')!.classList.toggle('hidden', view !== 'tests');
  document.getElementById('docs-view')!.classList.toggle('hidden', view !== 'docs');

  const graphOnly = view === 'graph';
  const listOnly = view === 'list';
  document.getElementById('sidebar')!.classList.toggle('hidden', view === 'workflow' || view === 'tests' || view === 'docs');
  document.getElementById('edge-filter-section')!.classList.toggle('hidden', !graphOnly);
  document.getElementById('layout-section')!.classList.toggle('hidden', !graphOnly);
  document.querySelectorAll('.list-only-section').forEach(el => {
    el.classList.toggle('hidden', !listOnly);
  });

  if (view === 'list') {
    List.render(state.filteredNodes, state.stalenessMap);
  } else if (view === 'graph' && Graph.getCy()) {
    requestAnimationFrame(() => Graph.getCy()?.resize());
  } else if (view === 'workflow') {
    Workflow.load();
  } else if (view === 'tests') {
    Tests.load();
  } else if (view === 'docs') {
    docsLoad();
  }
}

// ── Docs view load ──────────────────────────────────────────────────────────

async function docsLoad(): Promise<void> {
  const container = document.getElementById('docs-view');
  if (!container) return;

  if (DocApi.isDocsLoaded()) {
    renderDocsView();
    return;
  }

  container.innerHTML = '<div class="doc-status">Loading docs\u2026</div>';
  try {
    await DocApi.fetchDocs();
    renderDocsView();
  } catch (err: any) {
    container.innerHTML = `<div class="doc-status doc-error">Failed to load docs: ${esc(err.message)}</div>`;
  }
}

function renderDocsView(): void {
  const container = document.getElementById('docs-view');
  if (!container) return;
  const docs = DocApi.getDocs();
  const filtered = DocTree.applyFilters(docs);

  // Build the layout
  container.innerHTML = `
    <div class="doc-layout">
      <div class="doc-filter-panel" id="doc-filter-panel"></div>
      <div class="col-resize-handle"></div>
      <div class="doc-sidebar">
        <div class="doc-sidebar-header">
          <span>Documents (${filtered.length})</span>
          <div class="doc-sidebar-actions">
            <button class="doc-action-btn doc-action-btn-primary" id="doc-new-btn" title="New document">+</button>
            <button class="doc-action-btn" id="doc-refresh-btn" title="Refresh docs">\u21bb</button>
          </div>
        </div>
        <div class="doc-list" id="doc-list"></div>
      </div>
      <div class="col-resize-handle"></div>
      <div class="doc-content-area" id="doc-content-area">
        <div class="doc-status">\u2190 Select a document</div>
      </div>
    </div>`;

  // Render filter panel
  const filterPanel = document.getElementById('doc-filter-panel');
  if (filterPanel) DocTree.buildFilterPanel(filterPanel, () => renderDocsView());

  // Render folder tree
  const listEl = document.getElementById('doc-list');
  if (listEl) {
    DocTree.renderFolderTree(listEl, filtered, docsSelectDoc, DocApi.getSelectedDocId(),
      async (docId) => {
        const doc = docs.find(d => d.id === docId);
        if (!doc) return;
        const newName = prompt('New name:', doc.title);
        if (!newName || !newName.trim()) return;
        try {
          const data = await DocApi.renameDoc(docId, newName.trim());
          await DocApi.fetchDocs();
          if (data.new_id) docsSelectDoc(data.new_id);
          else renderDocsView();
        } catch (err: any) { alert('Error renaming: ' + err.message); }
      },
      async (docId) => {
        // Ensure subdirectories are loaded
        await DocApi.fetchSubdirs();
        const primary = DocApi.getPrimaryDocsDir();
        const allRoots = DocApi.getDocsDirs();
        const dirs = DocApi.getKnownSubdirs() ? [...DocApi.getKnownSubdirs()!].sort() : [primary];

        // Build options: for entries under the primary root show them
        // relative (primary-root itself -> "(root)").  Entries under an
        // alternate configured root keep their root prefix so the user can
        // tell them apart.
        const stripPrimary = (d: string): string | null => {
          const clean = primary.replace(/\/+$/, '');
          if (d === clean) return '';
          if (clean && d.startsWith(clean + '/')) return d.slice(clean.length + 1);
          return null;
        };
        const options = dirs.map(d => {
          const rel = stripPrimary(d);
          if (rel !== null) {
            return { value: rel, label: rel || '(root)' };
          }
          // Not under primary — check other roots.
          for (const r of allRoots) {
            const clean = r.replace(/\/+$/, '');
            if (clean && d === clean) return { value: clean, label: clean };
            if (clean && d.startsWith(clean + '/')) return { value: d, label: d };
          }
          return { value: d, label: d };
        });

        // Modal overlay with <select>
        const overlay = document.createElement('div');
        overlay.className = 'doc-move-overlay';
        overlay.innerHTML = `
          <div class="doc-move-dialog">
            <div class="doc-move-title">Move to folder</div>
            <select class="doc-move-select" size="${Math.min(options.length, 10)}">
              ${options.map(o => `<option value="${esc(o.value)}">${esc(o.label)}</option>`).join('')}
            </select>
            <div class="doc-move-btns">
              <button class="doc-move-cancel">Cancel</button>
            </div>
          </div>`;

        document.body.appendChild(overlay);

        const cleanup = () => overlay.remove();
        overlay.querySelector('.doc-move-cancel')!.addEventListener('click', cleanup);
        overlay.addEventListener('click', (e) => { if (e.target === overlay) cleanup(); });

        const sel = overlay.querySelector('.doc-move-select') as HTMLSelectElement;
        sel.addEventListener('dblclick', async () => {
          const dest = sel.value;
          cleanup();
          try {
            const data = await DocApi.moveDoc(docId, dest);
            await DocApi.fetchDocs();

            // Expand the target folder so the moved doc is visible.
            // If `dest` is already an absolute configured root (or begins
            // with one), use it verbatim; otherwise treat it as relative to
            // the primary root.
            let targetFolder: string;
            if (!dest) {
              targetFolder = primary;
            } else {
              let matched = false;
              for (const r of allRoots) {
                const clean = r.replace(/\/+$/, '');
                if (clean && (dest === clean || dest.startsWith(clean + '/'))) {
                  targetFolder = dest;
                  matched = true;
                  break;
                }
              }
              if (!matched) targetFolder = `${primary}/${dest}`;
            }
            targetFolder = targetFolder!;
            const expanded = DocTree.getExpandedFolders();
            expanded.add(targetFolder);
            const parts = targetFolder.split('/');
            for (let i = 1; i < parts.length; i++) {
              expanded.add(parts.slice(0, i + 1).join('/'));
            }

            if (data.new_id) docsSelectDoc(data.new_id);
            else renderDocsView();
          } catch (err: any) { alert('Error moving: ' + err.message); }
        });
      },
    );
  }

  // New doc button
  const newBtn = document.getElementById('doc-new-btn');
  if (newBtn) {
    newBtn.addEventListener('click', async () => {
      const title = prompt('New document title:');
      if (!title || !title.trim()) return;
      try {
        const data = await DocApi.createDoc(title.trim());
        await DocApi.fetchDocs();
        if (data.doc_id) docsSelectDoc(data.doc_id);
      } catch (err: any) { alert('Error: ' + err.message); }
    });
  }

  // Refresh button
  const refreshBtn = document.getElementById('doc-refresh-btn');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', async () => {
      try { await DocApi.refreshDocs(); renderDocsView(); }
      catch (err: any) { alert('Error: ' + err.message); }
    });
  }

  ColResize.init(container);

  // Restore selected doc
  const savedDocId = sessionStorage.getItem('cortex-doc-id');
  if (savedDocId && !DocApi.getSelectedDocId() && docs.some(d => d.id === savedDocId)) {
    docsSelectDoc(savedDocId);
  }
}

async function docsSelectDoc(docId: string): Promise<void> {
  sessionStorage.setItem('cortex-doc-id', docId);
  DocApi.setSelectedDocId(docId);

  // Reset editor state for new doc
  DocEditor.resetEditor();

  const contentArea = document.getElementById('doc-content-area');
  if (!contentArea) return;
  contentArea.innerHTML = '<div class="doc-status">Loading document\u2026</div>';

  try {
    const { raw, rendered } = await DocApi.fetchDoc(docId);
    DocEditor.setRenderedData(rendered);

    // Flatten nested raw sections to align indices with rendered sections
    if (raw && raw.sections) {
      raw.sections = _flattenRawSections(raw.sections);
      DocApi.setRawDoc(raw);
    }

    // Set re-render callback so doc-editor can trigger full re-renders
    DocEditor.setRerenderCallback(() => _renderSelectedDoc(contentArea));

    // Render the full interactive doc content
    _renderSelectedDoc(contentArea);
  } catch (err: any) {
    contentArea.innerHTML = `<div class="doc-status doc-error">Failed to load doc: ${esc(err.message)}</div>`;
  }

  // Re-render list to highlight active doc
  const listEl = document.getElementById('doc-list');
  if (listEl) {
    DocTree.renderFolderTree(listEl, DocTree.applyFilters(DocApi.getDocs()), docsSelectDoc, docId,
      () => {}, () => {},
    );
  }
}

function _renderSelectedDoc(contentArea: HTMLElement): void {
  DocEditor.renderDocContent(contentArea);
}

function _flattenRawSections(sections: any[]): any[] {
  const result: any[] = [];
  for (const sec of sections) {
    const { sections: children, ...rest } = sec;
    result.push(rest);
    if (children && children.length) result.push(..._flattenRawSections(children));
  }
  return result;
}

// ── Sidebar population ──────────────────────────────────────────────────────

function _populateSidebarFilters(meta: any): void {
  const typeContainer = document.getElementById('type-filters');
  if (!typeContainer) return;
  typeContainer.innerHTML = '';
  for (const [type, count] of Object.entries(meta.type_counts).sort()) {
    const color = Graph.NODE_TYPE_COLORS[type] || Graph.NODE_TYPE_DEFAULT;
    const label = document.createElement('label');
    label.className = 'filter-check';
    label.innerHTML =
      `<input type="checkbox" checked data-type="${type}">` +
      `<span class="type-dot" style="background:${color}"></span>` +
      `<span>${type}</span>` +
      `<span class="count">${count}</span>`;
    label.querySelector('input')!.addEventListener('change', _onTypeFilterChange);
    typeContainer.appendChild(label);
  }
  _buildTagFilterPanel(meta.tags);
}

// Abbreviated versions of the remaining App methods - they follow the same pattern
// as the original but use imports instead of globals

function _buildTagFilterPanel(tagList: string[]): void {
  const counts: Record<string, number> = {};
  for (const n of state.allNodes) for (const t of (n.tags || [])) counts[t] = (counts[t] || 0) + 1;
  if (Object.keys(counts).length === 0 && tagList) for (const t of tagList) counts[t] = counts[t] || 0;
  _allTags = Object.entries(counts).sort(([a], [b]) => a.localeCompare(b));
  _renderTagFilterPanel();
}

function _renderTagFilterPanel(): void {
  const panel = document.getElementById('tag-filter-panel');
  if (!panel) return;
  const fTags = state.filters.tags;
  const searchLower = (state.tagSearch || '').toLowerCase();
  const chips = [...fTags].map(t => {
    const count = (_allTags.find(([k]) => k === t) || [t, 0])[1];
    return `<span class="tag-fp-chip" data-tag="${escHtml(t)}"><span class="tag-fp-chip-label">${escHtml(t)}</span><span class="tag-fp-chip-count">${count}</span><span class="tag-fp-chip-x">&times;</span></span>`;
  }).join('');
  const byCount = [..._allTags].sort((a, b) => (b[1] as number) - (a[1] as number));
  const unselected = byCount.filter(([t]) => !fTags.has(t as string));
  const matching = searchLower ? unselected.filter(([t]) => (t as string).toLowerCase().includes(searchLower)) : unselected;
  const visible = matching.slice(0, _TAG_LIMIT);
  const overflow = matching.length - visible.length;
  const suggestions = visible.map(([t, count]) =>
    `<div class="tag-fp-suggestion" data-tag="${escHtml(t as string)}"><span class="tag-fp-sug-name">${escHtml(t as string)}</span><span class="tag-fp-sug-count">${count}</span></div>`,
  ).join('');
  panel.innerHTML =
    (chips ? `<div class="tag-fp-chips">${chips}</div>` : '') +
    `<input type="text" class="tag-fp-search" placeholder="Search tags\u2026" value="${escHtml(state.tagSearch || '')}">` +
    `<div class="tag-fp-suggestions">${suggestions}</div>` +
    (overflow > 0 ? `<button class="tag-fp-show-all">Show all (+${overflow} more)</button>` : '') +
    (fTags.size > 0 ? `<button class="tag-fp-clear">Clear tags</button>` : '');

  panel.querySelectorAll('.tag-fp-chip').forEach(el => {
    el.addEventListener('click', () => { fTags.delete((el as HTMLElement).dataset.tag || ''); _renderTagFilterPanel(); _applyFiltersAndRefresh(); });
  });
  panel.querySelectorAll('.tag-fp-suggestion').forEach(el => {
    el.addEventListener('click', () => { fTags.add((el as HTMLElement).dataset.tag || ''); state.tagSearch = ''; _renderTagFilterPanel(); _applyFiltersAndRefresh(); });
  });
  const searchInput = panel.querySelector('.tag-fp-search');
  if (searchInput) searchInput.addEventListener('input', e => { state.tagSearch = (e.target as HTMLInputElement).value; _renderTagFilterPanel(); });
  const showAll = panel.querySelector('.tag-fp-show-all');
  if (showAll) showAll.addEventListener('click', () => { _TAG_LIMIT = 999; _renderTagFilterPanel(); });
  const clearBtn = panel.querySelector('.tag-fp-clear');
  if (clearBtn) clearBtn.addEventListener('click', () => { fTags.clear(); state.tagSearch = ''; _TAG_LIMIT = 8; _renderTagFilterPanel(); _applyFiltersAndRefresh(); });
}

function _renderStalenessSummary(stalenessMap: StalenessMap): void {
  const el = document.getElementById('staleness-summary');
  if (!el) return;
  const catMap: Record<string, string> = {};
  for (const n of state.allNodes) catMap[n.id] = _nodeCategory(n);
  const byCat: Record<string, Record<string, number>> = {};
  const totals: Record<string, number> = {};
  for (const [nodeId, entry] of Object.entries(stalenessMap)) {
    const status = displayStaleness(entry);
    if (status === 'VERIFIED' || status === 'VERIFIED' || status === 'unknown') continue;
    const cat = catMap[nodeId] || 'code';
    if (!byCat[status]) byCat[status] = {};
    byCat[status][cat] = (byCat[status][cat] || 0) + 1;
    totals[status] = (totals[status] || 0) + 1;
  }
  if (Object.keys(totals).length === 0) { el.innerHTML = '<span class="staleness-all-clean">\u2713 All clean</span>'; return; }
  el.innerHTML = '';
  for (const [status, total] of Object.entries(totals)) {
    const cats = byCat[status];
    const parts = Object.entries(cats).sort(([a], [b]) => a.localeCompare(b)).map(([cat, n]) => `${n} ${cat}`).join(', ');
    const chip = document.createElement('button');
    chip.className = `staleness-summary-chip ${status}`;
    chip.title = parts;
    chip.textContent = `${total} ${status.replace(/_/g, ' ').toLowerCase()}`;
    chip.addEventListener('click', () => {
      const radio = document.querySelector(`input[name="staleness-radio"][value="${status}"]`) as HTMLInputElement | null;
      if (radio) { radio.checked = true; radio.dispatchEvent(new Event('change')); }
    });
    const detail = document.createElement('span');
    detail.className = 'staleness-chip-detail';
    detail.textContent = parts;
    chip.appendChild(detail);
    el.appendChild(chip);
  }
}

function _nodeCategory(node: AxiomNode): string {
  const st = node.subtype || '';
  if (st === 'docjson') return 'doc';
  if (st === 'test') return 'test';
  if (st === 'external_package') return 'external';
  return 'code';
}

function _populateStalenessFilter(): void {
  const values = [
    { value: 'all', label: 'All' }, { value: 'VERIFIED', label: 'Clean' }, { value: 'VERIFIED', label: 'Verified' },
    { value: 'CONTENT_UPDATED', label: 'Content updated' }, { value: 'DESC_UPDATED', label: 'Desc updated' },
    { value: 'NOT_FOUND', label: 'Not found' },
    { value: 'LINKED_STALE', label: 'Linked stale' }, { value: 'BROKEN_LINK', label: 'Broken link' },
  ];
  const container = document.getElementById('staleness-filters');
  if (!container) return;
  container.innerHTML = '';
  for (const { value, label } of values) {
    const lbl = document.createElement('label');
    lbl.className = 'status-radio';
    lbl.innerHTML = `<input type="radio" name="staleness-radio" value="${value}" ${value === 'all' ? 'checked' : ''}><span class="staleness-badge ${value === 'all' ? '' : value}">${label}</span>`;
    lbl.querySelector('input')!.addEventListener('change', _onStalenessFilterChange);
    container.appendChild(lbl);
  }
}

function _populateEdgeTypeFilters(edgeTypes: string[]): void {
  const container = document.getElementById('edge-filters');
  if (!container) return;
  container.innerHTML = '';
  for (const et of edgeTypes) {
    const color = Graph.EDGE_TYPE_COLORS[et] || Graph.EDGE_DEFAULT;
    const label = document.createElement('label');
    label.className = 'filter-check';
    label.innerHTML = `<input type="checkbox" checked data-edge-type="${et}"><span class="type-dot" style="background:${color}"></span><span>${et}</span>`;
    label.querySelector('input')!.addEventListener('change', _onEdgeTypeFilterChange);
    container.appendChild(label);
  }
}

const SUBTYPE_LABELS: Record<string, string> = {
  'function': 'Function',
  'test': 'Test',
  'docjson': 'Doc',
  'external_package': 'External',
  'module': 'Module',
};

function _populateSubtypeFilters(nodes: AxiomNode[]): void {
  const counts: Record<string, number> = {};
  for (const n of nodes) {
    const st = n.subtype || (n.node_type === 'composite_process' ? 'module' : 'function');
    counts[st] = (counts[st] || 0) + 1;
  }
  const container = document.getElementById('subtype-filters');
  if (!container) return;
  container.innerHTML = '';
  for (const [st, count] of Object.entries(counts).sort(([a], [b]) => a.localeCompare(b))) {
    const friendlyLabel = SUBTYPE_LABELS[st] || st;
    const label = document.createElement('label');
    label.className = 'filter-check';
    label.innerHTML = `<input type="checkbox" checked data-subtype="${st}"><span class="subtype-badge ${st}">${friendlyLabel}</span><span class="count">${count}</span>`;
    label.querySelector('input')!.addEventListener('change', _onSubtypeFilterChange);
    container.appendChild(label);
  }
}

function _renderSidebarStats(meta: any): void {
  const el = document.getElementById('sidebar-stats');
  if (el) el.innerHTML = `<div><strong>${meta.node_count}</strong> nodes</div><div><strong>${meta.edge_count}</strong> edges</div><div><strong>${meta.edge_types.length}</strong> edge types</div><div><strong>${meta.tags.length}</strong> tags</div>`;
}

// ── Filter event handlers ───────────────────────────────────────────────────

function _onTypeFilterChange(e: Event): void {
  const type = (e.target as HTMLElement).dataset.type || '';
  if ((e.target as HTMLInputElement).checked) state.filters.types.delete(type);
  else state.filters.types.add(type);
  _applyFiltersAndRefresh();
}

function _onSubtypeFilterChange(e: Event): void {
  const st = (e.target as HTMLElement).dataset.subtype || '';
  if ((e.target as HTMLInputElement).checked) state.filters.subtypes.delete(st);
  else state.filters.subtypes.add(st);
  _applyFiltersAndRefresh();
}

function _onEdgeTypeFilterChange(e: Event): void {
  const et = (e.target as HTMLElement).dataset.edgeType || '';
  state.filters.edgeTypes[et] = (e.target as HTMLInputElement).checked;
  Graph.applyEdgeFilter(state.filters.edgeTypes);
  if (state.filters.hideOrphans) Graph.hideOrphanNodes(new Set(state.filteredNodes.map(n => n.id)));
}

function _onHideOrphansChange(e: Event): void {
  state.filters.hideOrphans = (e.target as HTMLInputElement).checked;
  const baseIds = new Set(state.filteredNodes.map(n => n.id));
  if (state.filters.hideOrphans) Graph.hideOrphanNodes(baseIds);
  else Graph.filterNodes(baseIds);
}

function _onStalenessFilterChange(e: Event): void {
  state.filters.staleness = (e.target as HTMLInputElement).value;
  _applyFiltersAndRefresh();
}

// ── Filter logic ────────────────────────────────────────────────────────────

function _applyFilters(): void {
  let nodes = state.searchResultNodes || state.allNodes;
  if (state.filters.types.size > 0) nodes = nodes.filter(n => !state.filters.types.has(n.node_type));
  if (state.filters.subtypes.size > 0) nodes = nodes.filter(n => { const st = n.subtype || (n.node_type === 'composite_process' ? 'module' : 'function'); return !state.filters.subtypes.has(st); });
  if (state.filters.tags.size > 0) nodes = nodes.filter(n => { const tagSet = new Set(n.tags || []); for (const t of state.filters.tags) { if (!tagSet.has(t)) return false; } return true; });
  if (state.filters.staleness !== 'all') { const target = state.filters.staleness; nodes = nodes.filter(n => displayStaleness(state.stalenessMap[n.id]) === target); }
  if (!state.filters.showPrivate) nodes = nodes.filter(n => { const name = (n.title || '').split('.').pop() || ''; return !name.startsWith('_'); });
  if (!state.filters.showTests && state.meta && state.meta.test_paths) { const testPaths = state.meta.test_paths; if (testPaths.length > 0) nodes = nodes.filter(n => { const loc = n.location || ''; return !testPaths.some(tp => loc.startsWith(tp)); }); }
  if (state._sinceNodeIds) {
    nodes = nodes.filter(n => state._sinceNodeIds!.has(n.id));
    if (state._sinceDeletedNodes) { const liveIds = new Set(nodes.map(n => n.id)); for (const ghost of state._sinceDeletedNodes) { if (!liveIds.has(ghost.id)) nodes.push(ghost); } }
  }
  state.filteredNodes = nodes;
}

function _applyFiltersAndRefresh(): void {
  _applyFilters();
  if (state.view === 'list') List.render(state.filteredNodes, state.stalenessMap);
  else if (Graph.getCy() && !state.isLargeProject) {
    if (state._sinceNodeIds) {
      Graph.loadAll(state.filteredNodes, state.allEdges, state.stalenessMap, state.layout, state.stepEdgeLabels);
    } else {
      const visibleIds = new Set(state.filteredNodes.map(n => n.id));
      Graph.filterNodes(visibleIds);
      Graph.applyEdgeFilter(state.filters.edgeTypes);
      if (state.filters.hideOrphans && state.filters.staleness === 'all') Graph.hideOrphanNodes(visibleIds);
    }
  }
}

// ── Since filter ────────────────────────────────────────────────────────────

/**
 * Render the index-freshness banner in the Changed-Since panel.
 *  - resolved=false → warning that the requested commit isn't indexed.
 *  - behind > 0     → standing "index is N behind HEAD — rebuild" nudge.
 *  - otherwise      → hidden.
 * Informational only: never triggers a rebuild (the passive model stays).
 */
function _renderSinceBanner(opts: { resolved: boolean; requestedSha?: string | null; behind?: number | null }): void {
  const el = document.getElementById('since-banner');
  if (!el) return;
  const behind = typeof opts.behind === 'number' && opts.behind > 0 ? opts.behind : 0;
  const behindMsg = behind > 0 ? ` The index is ${behind} commit${behind === 1 ? '' : 's'} behind HEAD.` : '';
  if (!opts.resolved) {
    const reqSha = escHtml((opts.requestedSha || '').slice(0, 8));
    el.className = 'since-banner';
    el.innerHTML =
      `⚠ Commit <code>${reqSha}</code> isn't in the index — "changed since" ` +
      `can't be computed for it.${behindMsg} Rebuild the index ` +
      `(<code>axiom-graph build</code>) and try again.`;
  } else if (behind > 0) {
    el.className = 'since-banner nudge';
    el.innerHTML =
      `The index is ${behind} commit${behind === 1 ? '' : 's'} behind HEAD — ` +
      `rebuild (<code>axiom-graph build</code>) to see the latest.`;
  } else {
    el.className = 'since-banner hidden';
    el.innerHTML = '';
  }
}

async function applySinceFilter(
  type: string,
  customValue?: string,
  opts?: { commitSubject?: string | null },
): Promise<void> {
  const params = new URLSearchParams();
  if (type === 'checkpoint') { /* no params — default resolution */ }
  else if (type === 'last-commit') {
    // Use the most recent SHA from recent-shas endpoint
    try {
      const data = await apiFetch<{ shas: any[] }>('/api/history/recent-shas');
      const shas = data.shas || [];
      if (shas.length > 0) {
        params.set('sha', shas[0].sha);
        opts = { commitSubject: shas[0].commit_subject };
      }
    } catch { /* fall through to no-params default */ }
  }
  else if (type === '24h') params.set('timestamp', new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString());
  else if (type === '7d') params.set('timestamp', new Date(Date.now() - 7 * 24 * 60 * 60 * 1000).toISOString());
  else if (type === 'custom' && customValue) {
    if (/^[0-9a-f]{6,40}$/i.test(customValue)) params.set('sha', customValue);
    else params.set('timestamp', customValue);
  }
  setStatus('Filtering\u2026');
  try {
    const qs = params.toString();
    const data = await apiFetch('/api/history/since' + (qs ? '?' + qs : ''));
    if (data.resolved === false) {
      _renderSinceBanner({ resolved: false, requestedSha: data.requested_sha, behind: data.commits_behind_head });
      setStatus(`Commit ${(data.requested_sha || '').slice(0, 8)} isn't in the index`);
      _updateSinceUI(null);
      return;
    }
    if (!data.baseline_sha && !data.baseline_timestamp) {
      setStatus('No checkpoint found \u2014 use a SHA or date instead');
      _updateSinceUI(null);
      return;
    }
    _renderSinceBanner({ resolved: true, behind: data.commits_behind_head });
    state.sinceFilter = {
      type: type as any,
      value: customValue || null,
      commitSubject: opts?.commitSubject || null,
    };
    state._sinceNodeIds = new Set(data.node_ids);
    state._sinceBaselineSha = data.baseline_sha || null;
    state._sinceDeletedNodes = data.deleted_nodes || null;
    state._sinceUntilTimestamp = null;
    _updateSinceUI(type);
    _applyFiltersAndRefresh();
    const delCount = (data.deleted_nodes || []).length;
    const statusParts = [`${data.node_ids.length} changed nodes`];
    if (delCount > 0) statusParts.push(`${delCount} deleted`);
    setStatus(`Since filter: ${statusParts.join(', ')}`);
  } catch (err: any) { setStatus(`Since filter error: ${err.message}`); }
}

async function applySinceRange(
  sinceSha: string,
  untilSha: string,
  sinceSubject: string | null,
  untilSubject: string | null,
): Promise<void> {
  setStatus('Filtering range\u2026');
  try {
    const params = new URLSearchParams();
    params.set('sha', sinceSha);
    params.set('until_sha', untilSha);
    const data = await apiFetch('/api/history/since?' + params.toString());
    if (data.resolved === false) {
      _renderSinceBanner({ resolved: false, requestedSha: data.requested_sha, behind: data.commits_behind_head });
      setStatus(`Commit ${(data.requested_sha || '').slice(0, 8)} isn't in the index`);
      _updateSinceUI(null);
      return;
    }
    if (!data.baseline_sha && !data.baseline_timestamp) {
      setStatus('Range filter failed \u2014 could not resolve reference points');
      _updateSinceUI(null);
      return;
    }
    _renderSinceBanner({ resolved: true, behind: data.commits_behind_head });
    state.sinceFilter = {
      type: 'range',
      value: sinceSha,
      untilSha,
      untilTimestamp: data.until_timestamp || null,
      commitSubject: sinceSubject,
    };
    state._sinceNodeIds = new Set(data.node_ids);
    state._sinceBaselineSha = data.baseline_sha || null;
    state._sinceDeletedNodes = data.deleted_nodes || null;
    state._sinceUntilTimestamp = data.until_timestamp || null;
    _updateSinceUI('range');
    _applyFiltersAndRefresh();
    const delCount = (data.deleted_nodes || []).length;
    const statusParts = [`${data.node_ids.length} changed nodes`];
    if (delCount > 0) statusParts.push(`${delCount} deleted`);
    setStatus(`Range filter: ${statusParts.join(', ')}`);
  } catch (err: any) { setStatus(`Range filter error: ${err.message}`); }
}

function clearSinceFilter(): void {
  state.sinceFilter = null;
  state._sinceNodeIds = null;
  state._sinceBaselineSha = null;
  state._sinceDeletedNodes = null;
  state._sinceUntilTimestamp = null;
  _renderSinceBanner({ resolved: true, behind: 0 });
  _updateSinceUI(null);
  _applyFiltersAndRefresh();
  setStatus(`${state.meta ? state.meta.node_count : '?'} nodes \u00b7 ${state.meta ? state.meta.edge_count : '?'} edges`);
}

function _updateSinceUI(activeType: string | null): void {
  // Highlight active preset button
  document.querySelectorAll('.since-preset').forEach(btn => {
    btn.classList.toggle('active', (btn as HTMLElement).dataset.since === activeType);
  });

  // Toggle clear button
  const clearBtn = document.getElementById('btn-since-clear');
  if (clearBtn) clearBtn.classList.toggle('hidden', !activeType);

  // Update browse button label
  const browseBtn = document.getElementById('btn-since-browse');
  if (browseBtn) {
    browseBtn.textContent = activeType ? 'Change selection\u2026' : 'Browse\u2026';
  }

  // Render summary card
  const summary = document.getElementById('since-summary');
  if (summary) {
    if (activeType && state.sinceFilter) {
      summary.classList.remove('hidden');
      summary.innerHTML = _renderSinceSummary();
      // Wire the inline clear button
      const inlineClear = summary.querySelector('.since-summary-clear');
      if (inlineClear) {
        inlineClear.addEventListener('click', (e) => {
          e.stopPropagation();
          clearSinceFilter();
        });
      }
    } else {
      summary.classList.add('hidden');
      summary.innerHTML = '';
    }
  }

  // Update status text
  const statusEl = document.getElementById('since-status');
  if (statusEl) {
    if (activeType && state._sinceNodeIds) {
      const delCount = state._sinceDeletedNodes ? state._sinceDeletedNodes.length : 0;
      const parts = [`${state._sinceNodeIds.size} changed`];
      if (delCount > 0) parts.push(`${delCount} deleted`);
      statusEl.textContent = parts.join(', ');
    } else {
      statusEl.textContent = '';
    }
  }
}

function _renderSinceSummary(): string {
  const f = state.sinceFilter;
  if (!f) return '';

  const nodeCount = state._sinceNodeIds ? state._sinceNodeIds.size : 0;
  const delCount = state._sinceDeletedNodes ? state._sinceDeletedNodes.length : 0;
  let countText = `${nodeCount} changed node${nodeCount !== 1 ? 's' : ''}`;
  if (delCount > 0) countText += `, ${delCount} deleted`;

  if (f.type === 'range' && f.untilSha) {
    const sinceSha = escHtml((f.value || '').slice(0, 8));
    const untilSha = escHtml(f.untilSha.slice(0, 8));
    return (
      `<div class="since-summary-type">Range</div>` +
      `<div class="since-summary-sha">${sinceSha} &rarr; ${untilSha}</div>` +
      `<div class="since-summary-count">${countText}</div>` +
      `<button class="since-summary-clear">&times; Clear</button>`
    );
  }

  const typeLabels: Record<string, string> = {
    'checkpoint': 'Since last checkpoint',
    'last-commit': 'Since last commit',
    '24h': 'Last 24 hours',
    '7d': 'Last 7 days',
    'custom': 'Since commit',
  };
  const typeLabel = typeLabels[f.type] || 'Since';

  let shaHtml = '';
  if (f.value && /^[0-9a-f]{6,40}$/i.test(f.value)) {
    shaHtml = `<div class="since-summary-sha">${escHtml(f.value.slice(0, 8))}</div>`;
  } else if (state._sinceBaselineSha) {
    shaHtml = `<div class="since-summary-sha">${escHtml(state._sinceBaselineSha.slice(0, 8))}</div>`;
  }

  let subjectHtml = '';
  if (f.commitSubject) {
    subjectHtml = `<div class="since-summary-subject" title="${escHtml(f.commitSubject)}">${escHtml(f.commitSubject)}</div>`;
  }

  return (
    `<div class="since-summary-type">${typeLabel}</div>` +
    shaHtml +
    subjectHtml +
    `<div class="since-summary-count">${countText}</div>` +
    `<button class="since-summary-clear">&times; Clear</button>`
  );
}

// ── Search mode dropdown ────────────────────────────────────────────────────

function _initSearchModeDropdown(): void {
  const toggle = document.getElementById('search-mode-toggle')!;
  const dropdown = document.getElementById('search-mode-dropdown')!;
  const label = document.getElementById('search-mode-label')!;

  toggle.addEventListener('click', (e) => {
    e.stopPropagation();
    dropdown.classList.toggle('hidden');
  });

  dropdown.querySelectorAll('.search-mode-option').forEach(opt => {
    opt.addEventListener('click', (e) => {
      e.stopPropagation();
      const el = opt as HTMLElement;
      if (el.classList.contains('disabled')) return;
      const mode = el.dataset.mode as 'keyword' | 'semantic';
      state.searchMode = mode;
      label.textContent = mode === 'keyword' ? 'Keyword' : 'Semantic';
      dropdown.classList.add('hidden');
      // Re-run current query in new mode
      if (state.searchQuery) onSearch(state.searchQuery);
    });
  });

  // Close dropdown on outside click
  document.addEventListener('click', () => dropdown.classList.add('hidden'));

  _updateSearchModeDropdown();
}

function _updateSearchModeDropdown(): void {
  const semOpt = document.querySelector('.search-mode-option[data-mode="semantic"]') as HTMLElement | null;
  if (!semOpt) return;
  const emb = state.meta?.embeddings;
  if (emb && emb.available && emb.count > 0) {
    semOpt.classList.remove('disabled');
    semOpt.title = `${emb.count} embeddings (${Math.round(emb.coverage * 100)}% coverage)`;
  } else {
    semOpt.classList.add('disabled');
    semOpt.title = 'No embeddings built';
  }
}

// ── Graph notice / search / selectNode / check / rescan ─────────────────────

function _showGraphNotice(message: string, btnAction: () => void): void {
  const el = document.getElementById('graph-notice')!;
  el.innerHTML = `<p>${message}</p>`;
  const btn = document.createElement('button');
  btn.textContent = 'Load full graph anyway';
  btn.addEventListener('click', btnAction);
  el.appendChild(btn);
  el.classList.remove('hidden');
}

function _loadFullGraph(): void {
  document.getElementById('graph-notice')!.classList.add('hidden');
  state.isLargeProject = false;
  if (!Graph.getCy()) Graph.init(document.getElementById('cy')!);
  Graph.loadAll(state.filteredNodes, state.allEdges, state.stalenessMap, state.layout, state.stepEdgeLabels);
  setView('graph');
}

async function onSearch(query: string): Promise<void> {
  const q = query.trim();
  state.searchQuery = q;
  if (!q) {
    state.searchResultNodes = null;
    _applyFilters();
    if (state.view === 'list') List.render(state.filteredNodes, state.stalenessMap);
    else if (Graph.getCy()) Graph.clearHighlights();
    setStatus(`${state.meta ? state.meta.node_count : '?'} nodes \u00b7 ${state.meta ? state.meta.edge_count : '?'} edges`);
    return;
  }
  try {
    const data = await apiFetch(`/api/search?q=${encodeURIComponent(q)}&mode=${state.searchMode}`);
    state.searchResultNodes = data.nodes;
    _applyFilters();
    // Switch to list view from any tab
    if (state.view !== 'list') setView('list');
    List.render(state.filteredNodes, state.stalenessMap);
    const total = data.nodes.length;
    const shown = state.filteredNodes.length;
    const modeLabel = state.searchMode;
    if (shown < total) {
      setStatus(`${shown} of ${total} results (${modeLabel})`);
    } else {
      setStatus(`${total} results (${modeLabel})`);
    }
  } catch (err: any) { setStatus(`Search error: ${err.message}`); }
}

export async function selectNode(nodeId: string): Promise<void> {
  state.selectedNodeId = nodeId;
  try {
    const matchingWf = state.dflowWorkflows.find(w => w.cortex_node_id === nodeId);
    const basePromises: Promise<any>[] = [
      apiFetch(`/api/nodes/${encodeURIComponent(nodeId)}`),
      apiFetch(`/api/nodes/${encodeURIComponent(nodeId)}/history`),
      apiFetch(`/api/nodes/${encodeURIComponent(nodeId)}/tests`),
      apiFetch(`/api/nodes/${encodeURIComponent(nodeId)}/staleness-cause`).catch(() => null),
    ];
    if (matchingWf) basePromises.push(apiFetch(`/api/workflow/${matchingWf.id}/steps`));
    const results = await Promise.all(basePromises);
    const node = results[0], histData = results[1], testsData = results[2], stalenessCause = results[3];
    const wfSteps = matchingWf ? results[4] : null;
    const staleness = displayStaleness(state.stalenessMap[nodeId]);
    Detail.show(node, histData.history || [], staleness, wfSteps, testsData, stalenessCause);
    _renderDetailActions(nodeId);
    if (state.isLargeProject || !Graph.getCy()) {
      const nbData = await apiFetch(`/api/nodes/${encodeURIComponent(nodeId)}/neighborhood?depth=${state.depth}&direction=both`);
      const merged = { ...state.stalenessMap, ...(nbData.staleness || {}) };
      if (!Graph.getCy()) Graph.init(document.getElementById('cy')!);
      Graph.loadNeighborhood(nbData.nodes, nbData.edges, nodeId, merged, state.layout, state.stepEdgeLabels);
      document.getElementById('graph-notice')!.classList.add('hidden');
      if (state.view !== 'list') setView('graph');
    } else {
      Graph.selectNode(nodeId);
    }
  } catch (err) { console.error('selectNode error:', err); }
}

function navigateToNode(targetNodeId: string): void {
  // Resolve node type from loaded node list
  const node = state.allNodes ? state.allNodes.find((n: any) => n.id === targetNodeId) : null;
  const nodeType = node ? node.node_type : '';

  if (nodeType === 'doc' || nodeType === 'doc_section') {
    // Extract doc ID: for doc sections like "project::docs.foo.bar#section", use the doc part
    const docId = targetNodeId.includes('#') ? targetNodeId.split('#')[0] : targetNodeId;
    setView('docs');
    // Trigger doc selection after view switch renders
    setTimeout(() => {
      const listEl = document.getElementById('doc-list');
      if (listEl) {
        // Find the doc item and click it, or call docsSelectDoc if available
        const docItems = listEl.querySelectorAll('[data-doc-id]');
        for (const item of docItems) {
          if ((item as HTMLElement).dataset.docId === docId) {
            (item as HTMLElement).click();
            return;
          }
        }
      }
    }, 100);
  } else if (nodeType === 'test') {
    setView('tests');
    // Select in tests view after render
    selectNode(targetNodeId);
  } else {
    // Code nodes: switch to list view and open source
    setView('list');
    selectNode(targetNodeId);
    List.openSource(targetNodeId);
  }
}

function closeDetail(): void {
  state.selectedNodeId = null;
  Detail.hide();
  const el = document.getElementById('detail-actions');
  if (el) el.innerHTML = '';
  if (Graph.getCy()) Graph.deselectAll();
}

function _renderDetailActions(nodeId: string): void {
  const el = document.getElementById('detail-actions');
  if (!el) return;
  el.innerHTML = '';
  if (!state.isLargeProject && Graph.getCy()) {
    const expandBtn = document.createElement('button');
    expandBtn.className = 'detail-action-btn';
    expandBtn.title = 'Add N-hop neighbors';
    expandBtn.textContent = `Expand ${state.depth} hop${state.depth === 1 ? '' : 's'}`;
    expandBtn.addEventListener('click', () => _expandFromNode(nodeId));
    el.appendChild(expandBtn);
  }
}

function _expandFromNode(nodeId: string): void {
  const reachable = _bfsNeighborhood(nodeId, state.depth, 'both');
  const currentlyVisible = new Set<string>();
  if (Graph.getCy()) Graph.getCy().nodes(':visible').forEach((n: any) => currentlyVisible.add(n.id()));
  const toAdd = state.filteredNodes.filter(n => reachable.has(n.id) && !currentlyVisible.has(n.id));
  if (toAdd.length === 0) { setStatus('No new nodes found within that range.'); return; }
  const newVisible = new Set([...currentlyVisible, ...toAdd.map(n => n.id)]);
  Graph.filterNodes(newVisible);
  Graph.applyEdgeFilter(state.filters.edgeTypes);
  if (state.filters.hideOrphans) Graph.hideOrphanNodes(newVisible);
  setStatus(`Expanded: +${toAdd.length} node${toAdd.length === 1 ? '' : 's'} \u00b7 ${newVisible.size} total visible`);
}

function _bfsNeighborhood(seedId: string, maxDepth: number, direction: string): Set<string> {
  const out: Record<string, string[]> = {}, inc: Record<string, string[]> = {};
  for (const e of state.allEdges) { (out[e.from_id] = out[e.from_id] || []).push(e.to_id); (inc[e.to_id] = inc[e.to_id] || []).push(e.from_id); }
  const visited = new Set([seedId]);
  let frontier = [seedId];
  for (let d = 0; d < maxDepth; d++) {
    const next: string[] = [];
    for (const nid of frontier) {
      const neighbors: string[] = [];
      if (direction !== 'in') neighbors.push(...(out[nid] || []));
      if (direction !== 'out') neighbors.push(...(inc[nid] || []));
      for (const nb of neighbors) { if (!visited.has(nb)) { visited.add(nb); next.push(nb); } }
    }
    frontier = next;
    if (!frontier.length) break;
  }
  return visited;
}

async function check(): Promise<void> {
  const headerBtn = document.getElementById('btn-check') as HTMLButtonElement;
  const listBtn = document.getElementById('btn-list-check') as HTMLButtonElement | null;
  headerBtn.textContent = '\u2b21 Checking\u2026'; headerBtn.classList.add('loading'); headerBtn.disabled = true;
  if (listBtn) { listBtn.disabled = true; listBtn.textContent = '\u2b21 Checking\u2026'; }
  try {
    const data = await apiFetch('/api/check');
    Object.assign(state.stalenessMap, data.statuses);
    // Clear cause cache
    List.clearCauseCache();
    if (Graph.getCy()) Object.entries(data.statuses as StalenessMap).forEach(([id, entry]) => { const ele = Graph.getCy().getElementById(id); if (ele.length) ele.data('staleness', displayStaleness(entry as StalenessEntry)); });
    if (state.view === 'list') List.render(state.filteredNodes, state.stalenessMap);
    _renderStalenessSummary(state.stalenessMap);
    const s = data.summary || {};
    const parts = Object.entries(s).filter(([, n]) => (n as number) > 0).map(([k, n]) => `${n} ${k}`);
    setStatus('Check: ' + (parts.length ? parts.join(' \u00b7 ') : 'all clean'));
  } catch (err: any) { setStatus(`Check failed: ${err.message}`); }
  finally { headerBtn.textContent = '\u2b21 Check'; headerBtn.classList.remove('loading'); headerBtn.disabled = false; if (listBtn) { listBtn.disabled = false; listBtn.textContent = '\u2b21 Run Check'; } }
}

async function rescan(): Promise<void> {
  const btn = document.getElementById('btn-rescan') as HTMLButtonElement;
  btn.textContent = '\u21bb Scanning\u2026'; btn.classList.add('loading'); btn.disabled = true;
  try { await fetch('/api/rescan', { method: 'POST' }); setStatus('Rescan complete \u2014 reloading\u2026'); setTimeout(() => location.reload(), 900); }
  catch (err: any) { setStatus(`Rescan failed: ${err.message}`); btn.classList.remove('loading'); btn.disabled = false; btn.textContent = '\u21bb Rescan'; }
}

async function _loadDflowData(): Promise<void> {
  try {
    const [labelData, wfData] = await Promise.all([apiFetch('/api/workflow_graph_labels'), apiFetch('/api/workflows')]);
    if (labelData.available && labelData.labels.length > 0) {
      const map: Record<string, string> = {};
      for (const { from_cortex_node_id, to_cortex_node_id, step_number } of labelData.labels) map[`${from_cortex_node_id}|${to_cortex_node_id}`] = `Step ${step_number}`;
      state.stepEdgeLabels = map;
      if (Graph.getCy() && !state.isLargeProject) Graph.loadAll(state.filteredNodes, state.allEdges, state.stalenessMap, state.layout, state.stepEdgeLabels);
    }
    if (wfData.available) state.dflowWorkflows = wfData.workflows || [];
  } catch (err: any) { console.warn('Workflow graph data unavailable:', err.message); }
}

function _initProjectSwitcher(): void {
  const btn = document.getElementById('project-switcher-btn')!;
  const dropdown = document.getElementById('project-dropdown')!;

  const refreshProjectList = async () => {
    try {
      const data = await apiFetch('/api/projects');
      const list = document.getElementById('project-list')!;
      list.innerHTML = '';
      for (const p of data.projects) {
        const li = document.createElement('li');
        li.className = 'project-item' + (p.active ? ' active' : '');
        li.innerHTML = `<span class="project-item-name">${escHtml(p.name)}</span><span class="project-item-path">${escHtml(p.path)}</span>`;
        if (!p.active) li.addEventListener('click', async () => {
          dropdown.classList.add('hidden');
          setStatus('Switching project\u2026');
          try { await fetch('/api/projects/switch', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ project_root: p.path }) }).then(r => { if (!r.ok) return r.json().then(d => { throw new Error(d.detail || r.statusText); }); }); await _loadProject(); }
          catch (err: any) { setStatus(`Switch failed: ${err.message}`); }
        });
        list.appendChild(li);
      }
    } catch (err) { console.error('Failed to load project list:', err); }
  };

  btn.addEventListener('click', async () => {
    if (!dropdown.classList.contains('hidden')) { dropdown.classList.add('hidden'); return; }
    await refreshProjectList();
    dropdown.classList.remove('hidden');
  });
  document.addEventListener('click', e => { if (!(e.target as HTMLElement).closest('#project-switcher')) dropdown.classList.add('hidden'); });
  const addBtn = document.getElementById('project-add-btn')!;
  const addInput = document.getElementById('project-add-input') as HTMLInputElement;
  const register = async () => {
    const path = addInput.value.trim();
    if (!path) return;
    const errDiv = document.getElementById('project-add-error')!;
    errDiv.classList.add('hidden');
    try { const res = await fetch('/api/projects/register', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ project_root: path }) }); if (!res.ok) { const data = await res.json(); throw new Error(data.detail || res.statusText); } addInput.value = ''; await refreshProjectList(); }
    catch (err: any) { errDiv.textContent = err.message; errDiv.classList.remove('hidden'); }
  };
  addBtn.addEventListener('click', register);
  addInput.addEventListener('keydown', e => { if (e.key === 'Enter') register(); });
}

// ── ColResize ───────────────────────────────────────────────────────────────

export const ColResize = {
  init(container: HTMLElement): void {
    container.querySelectorAll('.col-resize-handle').forEach(h => ColResize._wire(h as HTMLElement));
  },
  _wire(handle: HTMLElement): void {
    handle.addEventListener('mousedown', e => {
      const useNext = handle.dataset.resizeTarget === 'next';
      const targetEl = (useNext ? handle.nextElementSibling : handle.previousElementSibling) as HTMLElement | null;
      if (!targetEl) return;
      const startX = e.clientX;
      const startWidth = targetEl.offsetWidth;
      handle.classList.add('dragging');
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
      function onMove(e: MouseEvent) { const dx = useNext ? (startX - e.clientX) : (e.clientX - startX); const newWidth = Math.max(100, startWidth + dx); targetEl!.style.width = newWidth + 'px'; targetEl!.style.flex = 'none'; }
      function onUp() { handle.classList.remove('dragging'); document.body.style.cursor = ''; document.body.style.userSelect = ''; document.removeEventListener('mousemove', onMove); document.removeEventListener('mouseup', onUp); }
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
      e.preventDefault();
    });
  },
};

// ── Entrypoint ──────────────────────────────────────────────────────────────

// Auto-init when DOM is ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => init());
} else {
  init();
}

// Also export for importmap access
function runMermaid(): Promise<void> {
  return DocEditor.runMermaid();
}
