// =============================================================================
// detail.ts -- Node detail drawer: 5-tab layout (Overview, Docs, API,
//              Relationships, History) with staleness cause, verify, edges
// =============================================================================

import type { AxiomNode, StalenessCause } from './types.js';
import { esc } from './view-utils.js';
import { NODE_TYPE_COLORS, NODE_TYPE_DEFAULT, EDGE_TYPE_COLORS, getCy } from './graph.js';

// Callback registration to avoid circular imports with app.ts
let selectNodeFn: ((nodeId: string) => void) | null = null;
let openSourceWithDiffFn: ((nodeId: string, sha: string) => void) | null = null;
let onVerifyCompleteFn: ((nodeId: string, status: string) => void) | null = null;
let navigateToNodeFn: ((nodeId: string) => void) | null = null;
let lookupNodeFn: ((nodeId: string) => { node_type: string; subtype?: string } | null) | null = null;

export function setDetailCallbacks(
  selectNode: (nodeId: string) => void,
  openSourceWithDiff?: (nodeId: string, sha: string) => void,
  onVerifyComplete?: (nodeId: string, status: string) => void,
  navigateToNode?: (nodeId: string) => void,
  lookupNode?: (nodeId: string) => { node_type: string; subtype?: string } | null,
): void {
  selectNodeFn = selectNode;
  openSourceWithDiffFn = openSourceWithDiff || null;
  onVerifyCompleteFn = onVerifyComplete || null;
  navigateToNodeFn = navigateToNode || null;
  lookupNodeFn = lookupNode || null;
}

// ── Tab state (persists across show() calls within session) ─────────────────

let activeTab = 'overview';
let cache: Record<string, any> = {};
let nodeId: string | null = null;
let nodeData: any = null;
let historyData: any[] | null = null;
let stalenessStatus: string | null = null;
let dflowStepsData: any = null;
let testsData: any = null;
let stalenessCause: StalenessCause | null = null;

// ── Public API ──────────────────────────────────────────────────────────────

export function show(
  node: AxiomNode,
  history: any[],
  staleness: string,
  dflowSteps: any,
  tests: any,
  cause: StalenessCause | null,
): void {
  nodeId = node.id;
  nodeData = node;
  historyData = history;
  stalenessStatus = staleness;
  dflowStepsData = dflowSteps;
  testsData = tests;
  stalenessCause = cause || null;
  cache = {};

  const panel = document.getElementById('detail-panel');
  if (panel) panel.classList.remove('hidden');

  const titleEl = document.getElementById('detail-title');
  if (titleEl) titleEl.textContent = node.title || node.id.split('::').pop() || null;

  renderTabs();
  activateTab(activeTab);
}

export function hide(): void {
  const panel = document.getElementById('detail-panel');
  if (panel) panel.classList.add('hidden');
  const tabs = document.getElementById('detail-tabs');
  if (tabs) tabs.innerHTML = '';
}

// ── Update staleness after verify ───────────────────────────────────────────

export function getNodeId(): string | null {
  return nodeId;
}

export function updateStaleness(status: string, cause: StalenessCause | null): void {
  stalenessStatus = status;
  stalenessCause = cause;
  if (activeTab === 'overview') activateTab('overview');
}

// ── Tab bar ─────────────────────────────────────────────────────────────────

function renderTabs(): void {
  const tabs = [
    { key: 'overview',      label: 'Overview' },
    { key: 'docs',          label: 'Docs' },
    { key: 'api',           label: 'API' },
    { key: 'relationships', label: 'Relationships' },
    { key: 'history',       label: 'History' },
  ];
  const container = document.getElementById('detail-tabs');
  if (!container) return;
  container.innerHTML = '';
  for (const { key, label } of tabs) {
    const btn = document.createElement('button');
    btn.className = 'detail-tab-btn' + (key === activeTab ? ' active' : '');
    btn.dataset.tab = key;
    btn.textContent = label;
    btn.addEventListener('click', () => activateTab(key));
    container.appendChild(btn);
  }
}

function activateTab(tabKey: string): void {
  activeTab = tabKey;

  document.querySelectorAll('.detail-tab-btn').forEach(btn => {
    (btn as HTMLElement).classList.toggle('active', (btn as HTMLElement).dataset.tab === tabKey);
  });

  const content = document.getElementById('detail-content');
  if (!content) return;
  content.scrollTop = 0;

  const renderers: Record<string, () => string> = {
    overview:      renderOverviewTab,
    docs:          renderDocsTab,
    api:           renderApiTab,
    relationships: renderRelationshipsTab,
    history:       renderHistoryTab,
  };

  const html = (renderers[tabKey] || renderers.overview)();
  content.innerHTML = html;
  wireTabEvents(content);
}

// ── Wire events after tab render ────────────────────────────────────────────

function wireTabEvents(content: HTMLElement): void {
  // Copy buttons
  content.querySelectorAll('.copy-btn[data-copy]').forEach(btn => {
    btn.addEventListener('click', () => {
      const text = (btn as HTMLElement).dataset.copy || '';
      navigator.clipboard.writeText(text).then(() => {
        const orig = btn.textContent;
        btn.textContent = '\u2713';
        setTimeout(() => { btn.textContent = orig; }, 1200);
      });
    });
  });

  // Verify button expand/collapse
  const verifyBtn = content.querySelector('#detail-verify-btn');
  if (verifyBtn) {
    verifyBtn.addEventListener('click', () => {
      const expand = content.querySelector('.detail-verify-expand');
      if (expand) expand.classList.toggle('hidden');
    });
  }

  // Verify confirm
  const confirmBtn = content.querySelector('#detail-verify-confirm');
  if (confirmBtn) {
    confirmBtn.addEventListener('click', () => doVerify(content));
  }

  // Verify cancel
  const cancelBtn = content.querySelector('#detail-verify-cancel');
  if (cancelBtn) {
    cancelBtn.addEventListener('click', () => {
      const expand = content.querySelector('.detail-verify-expand');
      if (expand) expand.classList.add('hidden');
    });
  }

  // Clickable SHA entries in history tab
  content.querySelectorAll('.detail-sha-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const nid = (btn as HTMLElement).dataset.nodeId;
      const sha = (btn as HTMLElement).dataset.sha;
      if (nid && sha && openSourceWithDiffFn) {
        openSourceWithDiffFn(nid, sha);
      }
    });
  });

  // Jump buttons — contextual navigation
  content.querySelectorAll('.detail-jump-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const nid = (btn as HTMLElement).dataset.nodeId;
      if (!nid) return;
      if (navigateToNodeFn) navigateToNodeFn(nid);
      else if (selectNodeFn) selectNodeFn(nid);
    });
  });

  // View Source button — navigates based on node type (code → list, doc → docs, test → tests)
  content.querySelectorAll('.detail-view-source-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const nid = (btn as HTMLElement).dataset.nodeId;
      if (nid && navigateToNodeFn) navigateToNodeFn(nid);
    });
  });

  // Edge group collapse/expand
  content.querySelectorAll('.edge-group-header').forEach(header => {
    header.addEventListener('click', () => {
      const group = (header as HTMLElement).closest('.edge-group');
      if (!group) return;
      group.classList.toggle('collapsed');
    });
  });
}

// ── Verify action ───────────────────────────────────────────────────────────

async function doVerify(content: HTMLElement): Promise<void> {
  const reasonInput = content.querySelector('.detail-verify-reason') as HTMLInputElement | null;
  const reason = reasonInput ? reasonInput.value.trim() : '';
  const nid = nodeId;
  if (!nid) return;

  const confirmBtn = content.querySelector('#detail-verify-confirm') as HTMLButtonElement | null;
  if (confirmBtn) { confirmBtn.disabled = true; confirmBtn.textContent = 'Verifying\u2026'; }

  try {
    const res = await fetch(`/api/nodes/${encodeURIComponent(nid)}/verify`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason: reason || null, verified_by: 'human' }),
    });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);

    // Re-fetch staleness cause
    const causeRes = await fetch(`/api/nodes/${encodeURIComponent(nid)}/staleness-cause`);
    if (causeRes.ok) {
      stalenessCause = await causeRes.json();
    }

    stalenessStatus = 'VERIFIED';

    // Notify app to update stalenessMap and re-render summary
    if (onVerifyCompleteFn && nid) onVerifyCompleteFn(nid, 'VERIFIED');

    // Update graph node if available
    const cy = getCy();
    if (cy) {
      const ele = cy.getElementById(nid);
      if (ele.length) ele.data('staleness', 'VERIFIED');
    }

    if (activeTab === 'overview') activateTab('overview');
  } catch (err) {
    console.error('Verify failed:', err);
    if (confirmBtn) { confirmBtn.disabled = false; confirmBtn.textContent = 'Confirm'; }
  }
}

// =============================================================================
// TAB RENDERERS
// =============================================================================

function renderOverviewTab(): string {
  const node = nodeData;
  const staleness = stalenessStatus;
  const cause = stalenessCause;
  if (!node) return '';
  const typeColor = NODE_TYPE_COLORS[node.node_type] || NODE_TYPE_DEFAULT;
  const parts: string[] = [];

  // Badges row
  parts.push(
    `<div class="detail-section">` +
      `<span class="type-badge" style="background:${typeColor}">${esc(node.node_type)}</span>` +
      (node.subtype
        ? `<span class="type-badge" style="background:#888;font-size:10px">${esc(node.subtype)}</span>`
        : '') +
      `<span class="staleness-badge ${staleness}">${staleness}</span>` +
      `<span style="font-size:10px;color:#999;margin-left:8px">${esc(node.status)}</span>` +
    `</div>`,
  );

  // ID
  parts.push(labeledBlock(
    'ID',
    `<div class="detail-location">${esc(node.id)}` +
    `<button class="copy-btn" data-copy="${esc(node.id)}">copy</button></div>`,
  ));

  // Location
  const locLine = node.level_3_location
    ? `${esc(node.location)}<br><span style="font-size:10px;color:#999">${esc(node.level_3_location)}</span>`
    : esc(node.location);
  parts.push(labeledBlock(
    'Location',
    `<div class="detail-location">${locLine}` +
    `<button class="copy-btn" data-copy="${esc(node.location)}">copy</button></div>`,
  ));

  // Tags
  if (node.tags && node.tags.length > 0) {
    const chips = node.tags.map((t: string) => `<span class="tag-chip">${esc(t)}</span>`).join('');
    parts.push(labeledBlock('Tags', `<div class="flex flex-wrap gap-4">${chips}</div>`));
  }

  // Summary (level_0)
  if (node.level_0) {
    parts.push(labeledBlock(
      'Summary',
      `<div class="detail-level-block">${esc(node.level_0)}</div>`,
    ));
  }

  // Staleness cause
  if (cause) {
    const isClean = cause.status === 'VERIFIED' || cause.status === 'VERIFIED';
    parts.push(labeledBlock(
      'Staleness Cause',
      `<div class="detail-cause-block${isClean ? ' clean' : ''}">${esc((cause as any).cause)}</div>` +
      renderStaleLinkedNodes(cause),
    ));
  }

  // Verification attribution
  if (cause && (cause as any).verification) {
    const v = (cause as any).verification;
    const when = v.verified_at ? new Date(v.verified_at).toLocaleString() : 'unknown';
    const by = v.verified_by || 'unknown';
    const reason = v.reason ? ` \u2014 ${esc(v.reason)}` : '';
    parts.push(labeledBlock(
      'Verification',
      `<div class="detail-verification-attr">` +
        `Verified by <strong>${esc(by)}</strong> on ${esc(when)}${reason}` +
      `</div>`,
    ));
  }

  // Verify button
  if (staleness && staleness !== 'VERIFIED' && staleness !== 'VERIFIED' && staleness !== 'NOT_FOUND' && staleness !== 'unknown') {
    parts.push(
      `<div class="detail-verify-form">` +
        `<button id="detail-verify-btn" class="detail-action-btn primary">Mark Verified</button>` +
        `<div class="detail-verify-expand hidden">` +
          `<input type="text" class="detail-verify-reason" placeholder="Reason (optional)\u2026">` +
          `<button id="detail-verify-confirm" class="detail-action-btn primary">Confirm</button>` +
          `<button id="detail-verify-cancel" class="detail-action-btn">Cancel</button>` +
        `</div>` +
      `</div>`,
    );
  }

  return parts.join('');
}

function renderStaleLinkedNodes(cause: any): string {
  if (!cause || !cause.details || !cause.details.stale_linked_nodes) return '';
  const nodes = cause.details.stale_linked_nodes;
  if (nodes.length === 0) return '';
  const links = nodes.map((nid: string) => {
    const short = nid.split('::').pop();
    return `<button class="detail-jump-btn" data-node-id="${esc(nid)}">\u2192 ${esc(short)}</button>`;
  }).join(' ');
  return `<div style="margin-top:6px">${links}</div>`;
}

// ── Docs Tab ────────────────────────────────────────────────────────────────

function renderDocsTab(): string {
  const node = nodeData;
  if (!node) return '';
  const parts: string[] = [];

  if (node.level_1) {
    parts.push(labeledBlock(
      'Level 1 \u2014 Description',
      `<div class="detail-level-block">${esc(node.level_1)}</div>`,
    ));
  }

  if (node.level_2) {
    parts.push(labeledBlock(
      'Level 2 \u2014 Details',
      `<div class="detail-level-block level-2">${esc(node.level_2)}</div>`,
    ));
  }

  if (parts.length === 0) {
    parts.push('<div class="detail-tab-empty">No documentation available.</div>');
  }

  return parts.join('');
}

// ── API Tab ─────────────────────────────────────────────────────────────────

function renderApiTab(): string {
  const node = nodeData;
  if (!node) return '';
  const parts: string[] = [];

  if (node.dflow_meta) {
    const autodocHtml = renderAutodocInner(node.dflow_meta);
    if (autodocHtml) parts.push(labeledBlock('Interface', autodocHtml));
  }

  if (node.level_2) {
    parts.push(labeledBlock(
      'Docstring',
      `<div class="detail-level-block level-2">${esc(node.level_2)}</div>`,
    ));
  }

  const mergedSteps = mergeSteps(node.level_steps, dflowStepsData);
  if (mergedSteps.length > 0) {
    const stepsHtml = mergedSteps.map(s => renderStepItem(s)).join('');
    parts.push(labeledBlock(`Steps (${mergedSteps.length})`, stepsHtml));
  }

  if (node.location || node.node_type === 'doc' || node.node_type === 'doc_section') {
    parts.push(
      `<div class="detail-section">` +
        `<button class="detail-action-btn detail-view-source-btn" data-node-id="${esc(node.id)}" title="Opens source code panel">View Source</button>` +
      `</div>`,
    );
  }

  if (parts.length === 0) {
    parts.push('<div class="detail-tab-empty">No API information available.</div>');
  }

  return parts.join('');
}

function mergeSteps(cortexSteps: any, dflowData: any): any[] {
  const merged = new Map<string, any>();

  if (cortexSteps && cortexSteps.length > 0) {
    for (const s of cortexSteps) {
      const key = String(s.step_num);
      merged.set(key, {
        step_number: s.step_num,
        name: s.name,
        purpose: s.purpose,
        inputs: s.inputs,
        outputs: s.outputs,
        critical: s.critical,
        source: 'cortex',
      });
    }
  }

  if (dflowData && dflowData.steps && dflowData.steps.length > 0) {
    for (const s of dflowData.steps) {
      const key = String(s.step_number);
      merged.set(key, {
        step_number: s.step_number,
        name: s.name,
        purpose: s.purpose,
        inputs: s.inputs,
        outputs: s.outputs,
        critical: s.critical,
        is_auto: s.is_auto,
        cortex_node_id: s.cortex_node_id,
        calls_function: s.calls_function,
        source: merged.has(key) ? 'merged' : 'dflow',
      });
    }
  }

  return Array.from(merged.values()).sort((a, b) => Number(a.step_number) - Number(b.step_number));
}

function renderStepItem(s: any): string {
  const isMinor = String(s.step_number).includes('.');
  const io =
    (s.inputs  ? `<div class="step-io" style="color:#27ae60">in: ${esc(s.inputs)}</div>`  : '') +
    (s.outputs ? `<div class="step-io" style="color:#4a90d9">out: ${esc(s.outputs)}</div>` : '');
  const crit = s.critical
    ? `<div class="step-critical">\u26a0 ${esc(s.critical)}</div>` : '';
  const autoBadge = s.is_auto
    ? '<span class="type-badge" style="background:#9b59b6;font-size:9px">auto</span> ' : '';
  const sourceBadge = s.source === 'cortex'
    ? '<span class="type-badge" style="background:#586069;font-size:9px">cortex</span> '
    : s.source === 'merged'
      ? '<span class="type-badge" style="background:#2ea043;font-size:9px">merged</span> '
      : '';
  let jumpBtn = '';
  if (s.cortex_node_id) {
    const nid  = esc(s.cortex_node_id);
    const name = esc(s.calls_function || s.cortex_node_id.split('::').pop());
    jumpBtn = `<button class="detail-jump-btn" data-node-id="${nid}">\u2192 ${name}</button>`;
  } else if (s.calls_function) {
    jumpBtn = `<span style="font-size:10px;color:#aaa">${esc(s.calls_function)}</span>`;
  }
  return (
    `<div class="step-item${isMinor ? ' step-item-minor' : ''}">` +
      `<div class="step-num">Step ${esc(String(s.step_number))}</div>` +
      `<div class="step-name">${autoBadge}${sourceBadge}${esc(s.name)}</div>` +
      (s.purpose ? `<div class="step-purpose">${esc(s.purpose)}</div>` : '') +
      io + crit + jumpBtn +
    `</div>`
  );
}

// ── Relationships Tab ───────────────────────────────────────────────────────

function renderRelationshipsTab(): string {
  const parts: string[] = [];

  if (testsData && testsData.tests && testsData.tests.length > 0) {
    parts.push(renderTestCoverage(testsData.tests));
  }

  if (!cache.edges) {
    parts.push('<div class="detail-tab-loading">Loading edges\u2026</div>');
    fetchEdges();
    return parts.join('');
  }

  const edges = cache.edges;

  if (edges.inbound && edges.inbound.length > 0) {
    parts.push(labeledBlock(
      `Inbound (${edges.inbound.length})`,
      renderEdgeGroups(edges.inbound, 'inbound'),
    ));
  }

  if (edges.outbound && edges.outbound.length > 0) {
    parts.push(labeledBlock(
      `Outbound (${edges.outbound.length})`,
      renderEdgeGroups(edges.outbound, 'outbound'),
    ));
  }

  if (parts.length === 0) {
    parts.push('<div class="detail-tab-empty">No relationships found.</div>');
  }

  return parts.join('');
}

async function fetchEdges(): Promise<void> {
  const nid = nodeId;
  if (!nid) return;
  try {
    const res = await fetch(`/api/nodes/${encodeURIComponent(nid)}/edges`);
    if (res.status === 404) {
      // Node not in DB (e.g. doc section) — treat as empty
      if (nodeId === nid) cache.edges = { inbound: [], outbound: [] };
    } else if (!res.ok) {
      throw new Error(`${res.status}`);
    } else {
      const data = await res.json();
      if (nodeId === nid) cache.edges = data;
    }
    // Re-render if still on this tab
    if (nodeId === nid && activeTab === 'relationships') {
      activateTab('relationships');
    }
  } catch (err) {
    console.error('Edge fetch failed:', err);
    // Cache empty so tab doesn't re-fetch endlessly
    if (nodeId === nid) cache.edges = { inbound: [], outbound: [] };
    if (nodeId === nid && activeTab === 'relationships') {
      activateTab('relationships');
    }
  }
}

const NODE_TYPE_BADGE_CLASS: Record<string, string> = {
  'function': 'node-type-fn', 'class': 'node-type-cls', 'module': 'node-type-mod',
  'doc': 'node-type-doc', 'doc_section': 'node-type-doc', 'test': 'node-type-test',
};
const NODE_TYPE_SHORT: Record<string, string> = {
  'function': 'fn', 'class': 'cls', 'module': 'mod',
  'doc': 'doc', 'doc_section': 'doc', 'test': 'test',
};

function renderEdgeGroups(edges: any[], direction: string): string {
  const groups: Record<string, any[]> = {};
  for (const e of edges) {
    const t = e.edge_type || 'unknown';
    if (!groups[t]) groups[t] = [];
    groups[t].push(e);
  }

  const sorted = Object.entries(groups).sort((a, b) => b[1].length - a[1].length);
  const colHeader = direction === 'inbound' ? 'Source' : 'Target';

  return sorted.map(([edgeType, edgeList]) => {
    const edgeColor = EDGE_TYPE_COLORS[edgeType] || '#888';
    const collapsed = edgeList.length > 3 ? ' collapsed' : '';
    const chevron = edgeList.length > 3 ? '\u25B6' : '\u25BC';
    const rows = edgeList.map(e => {
      const otherId = direction === 'inbound' ? e.from_id : e.to_id;
      const shortName = otherId.split('::').pop();
      const info = lookupNodeFn ? lookupNodeFn(otherId) : null;
      const nt = info ? info.node_type : '';
      const badgeClass = NODE_TYPE_BADGE_CLASS[nt] || 'node-type-default';
      const shortType = NODE_TYPE_SHORT[nt] || nt || '';
      const subtype = info && info.subtype ? esc(info.subtype) : '';
      return (
        `<tr>` +
          `<td><button class="detail-jump-btn" data-node-id="${esc(otherId)}">${esc(shortName)}</button></td>` +
          `<td>${shortType ? `<span class="node-type-pill ${badgeClass}">${esc(shortType)}</span>` : ''}</td>` +
          `<td class="edge-subtype-cell">${subtype}</td>` +
        `</tr>`
      );
    }).join('');
    return (
      `<div class="edge-group${collapsed}">` +
        `<div class="edge-group-header">` +
          `<span class="edge-group-chevron">${chevron}</span>` +
          `<span class="detail-edge-type-badge" style="background:${edgeColor}">${esc(edgeType)}</span>` +
          `<span class="edge-group-count">(${edgeList.length})</span>` +
        `</div>` +
        `<div class="edge-group-body">` +
          `<table class="edge-group-table">` +
            `<colgroup><col class="eg-col-name"><col class="eg-col-type"><col class="eg-col-subtype"></colgroup>` +
            `<thead><tr><th>${colHeader}</th><th>Type</th><th>Subtype</th></tr></thead>` +
            `<tbody>${rows}</tbody>` +
          `</table>` +
        `</div>` +
      `</div>`
    );
  }).join('');
}

// ── History Tab ─────────────────────────────────────────────────────────────

function renderHistoryTab(): string {
  const history = historyData;
  const cause = stalenessCause;
  const parts: string[] = [];

  if (cause && (cause as any).verification) {
    const v = (cause as any).verification;
    const when = v.verified_at ? new Date(v.verified_at).toLocaleString() : 'unknown';
    const by = v.verified_by || 'unknown';
    const reason = v.reason ? esc(v.reason) : 'No reason provided';
    const hash = v.code_hash_at ? `<br>Code hash at verification: <code>${esc(v.code_hash_at)}</code>` : '';
    parts.push(labeledBlock(
      'Verification Record',
      `<div class="detail-verification-attr">` +
        `Verified by <strong>${esc(by)}</strong> on ${esc(when)}<br>` +
        `Reason: ${reason}${hash}` +
      `</div>`,
    ));
  }

  if (history && history.length > 0) {
    const renames = history.filter((h: any) => (h.change_type || '').toUpperCase().includes('RENAME'));
    if (renames.length > 0) {
      const rows = renames.map((h: any) =>
        `<tr>` +
          `<td>${esc((h.scanned_at || '').slice(0, 10))}</td>` +
          `<td>${esc(h.change_type || '')}</td>` +
          `<td style="font-size:10px;color:#999">${esc((h.git_sha || '').slice(0, 8))}</td>` +
        `</tr>`,
      ).join('');
      parts.push(labeledBlock(
        'Rename History',
        `<table class="history-table">` +
          `<thead><tr><th>Date</th><th>Change</th><th>SHA</th></tr></thead>` +
          `<tbody>${rows}</tbody>` +
        `</table>`,
      ));
    }
  }

  if (history && history.length > 0) {
    const filtered = history.filter((h: any) => (h.change_type || '').toUpperCase() !== 'CHECKPOINT');
    const rows = filtered.map((h: any) => {
      const sha = (h.git_sha || '').slice(0, 8);
      const subject = h.commit_subject || '';
      const ct = (h.change_type || '').toUpperCase();
      const badgeClass = ct.includes('VERIFIED') || ct === 'BECAME_VERIFIED' || ct === 'LINK_BECAME_VERIFIED' ? 'history-badge-verified'
        : ct.startsWith('BECAME_LINKED') || ct.startsWith('BECAME_CONTENT') || ct.startsWith('BECAME_NOT') || ct.startsWith('BECAME_BROKEN') || ct.startsWith('BECAME_DESC') ? 'history-badge-staleness'
        : ct === 'CONTENT_ONLY' || ct === 'CONTENT_AND_DESC' ? 'history-badge-content'
        : ct === 'DESC_ONLY' ? 'history-badge-desc'
        : ct.startsWith('LINK_') ? 'history-badge-link'
        : 'history-badge-initial';
      const shaCell = sha
        ? `<td><button class="detail-sha-btn" data-node-id="${esc(nodeId || '')}" data-sha="${esc(h.git_sha || '')}" title="${esc(subject || 'View source at this SHA')}">${esc(sha)}</button></td>`
        : `<td style="font-size:10px;color:#999"></td>`;
      return (
        `<tr>` +
          `<td>${esc((h.scanned_at || '').slice(0, 10))}</td>` +
          `<td><span class="history-badge ${badgeClass}">${esc(h.change_type || '')}</span></td>` +
          shaCell +
        `</tr>`
      );
    }).join('');
    parts.push(labeledBlock(
      `Node History (${filtered.length})`,
      `<table class="history-table">` +
        `<thead><tr><th>Date</th><th>Event</th><th>SHA</th></tr></thead>` +
        `<tbody>${rows}</tbody>` +
      `</table>`,
    ));
  }

  if (parts.length === 0) {
    parts.push('<div class="detail-tab-empty">No history available.</div>');
  }

  return parts.join('');
}

// ── Autodoc renderer ────────────────────────────────────────────────────────

function renderAutodocInner(meta: any): string {
  const sections: string[] = [];

  if (meta.params && meta.params.length > 0) {
    const rows = meta.params.map((p: any) => {
      const typeBadge = p.type ? `<span class="autodoc-type">${esc(p.type)}</span>` : '';
      const optBadge = p.optional ? '<span class="autodoc-optional">optional</span>' : '';
      const defaultVal = p.default ? `<span class="autodoc-default">= ${esc(p.default)}</span>` : '';
      return (
        `<tr>` +
          `<td class="autodoc-name">${esc(p.name)} ${optBadge}</td>` +
          `<td>${typeBadge} ${defaultVal}</td>` +
          `<td class="autodoc-desc">${esc(p.desc || '')}</td>` +
        `</tr>`
      );
    }).join('');
    sections.push(
      `<div class="autodoc-block">` +
        `<div class="autodoc-heading">Parameters</div>` +
        `<table class="autodoc-table"><tbody>${rows}</tbody></table>` +
      `</div>`,
    );
  }

  if (meta.returns && meta.returns.length > 0) {
    const rows = meta.returns.map((r: any) => {
      const typeBadge = r.type ? `<span class="autodoc-type">${esc(r.type)}</span> ` : '';
      return `<div class="autodoc-return-item">${typeBadge}${esc(r.desc || '')}</div>`;
    }).join('');
    sections.push(
      `<div class="autodoc-block">` +
        `<div class="autodoc-heading">Returns</div>` +
        rows +
      `</div>`,
    );
  }

  if (meta.raises_doc && meta.raises_doc.length > 0) {
    const rows = meta.raises_doc.map((r: any) => {
      const typeBadge = r.type ? `<span class="autodoc-raises-type">${esc(r.type)}</span> ` : '';
      return `<div class="autodoc-raises-item">${typeBadge}${esc(r.desc || '')}</div>`;
    }).join('');
    sections.push(
      `<div class="autodoc-block">` +
        `<div class="autodoc-heading">Raises</div>` +
        rows +
      `</div>`,
    );
  } else if (meta.raises && meta.raises.length > 0) {
    const chips = meta.raises.map((r: string) =>
      `<span class="autodoc-raises-type">${esc(r)}</span>`,
    ).join(' ');
    sections.push(
      `<div class="autodoc-block">` +
        `<div class="autodoc-heading">Raises</div>` +
        `<div>${chips}</div>` +
      `</div>`,
    );
  }

  return sections.length > 0 ? sections.join('') : '';
}

// ── Test coverage renderer ──────────────────────────────────────────────────

function renderTestCoverage(tests: any[]): string {
  const statusIcons: Record<string, string> = {
    'PASS': '\u2705', 'passed': '\u2705', 'pass': '\u2705',
    'FAIL': '\u274c', 'failed': '\u274c', 'fail': '\u274c',
    'ERROR': '\ud83d\udca5', 'error': '\ud83d\udca5',
    'SKIP': '\u23ed\ufe0f', 'skipped': '\u23ed\ufe0f', 'skip': '\u23ed\ufe0f',
    'unknown': '\u2753',
  };

  const rows = tests.map(t => {
    const icon = statusIcons[t.status] || statusIcons['unknown'];
    const shortName = (t.name || '').split('.').pop();
    const loc = t.location ? t.location.replace(/\\/g, '/') : '';
    const locShort = loc.split('/').pop();
    const jumpBtn = t.test_node_id
      ? `<button class="detail-jump-btn" data-node-id="${esc(t.test_node_id)}" title="Jump to test node">\u2192</button>`
      : '';
    const srcBadge = t.source === 'dflow_covers'
      ? '<span class="type-badge" style="background:#9b59b6;font-size:9px">covers</span> ' : '';
    return (
      `<tr class="test-coverage-row">` +
        `<td class="test-status-icon">${icon}</td>` +
        `<td class="test-name">${srcBadge}${esc(shortName)}</td>` +
        `<td class="test-location" title="${esc(loc)}">${esc(locShort)}</td>` +
        `<td>${jumpBtn}</td>` +
      `</tr>`
    );
  }).join('');

  return labeledBlock(
    `Test Coverage (${tests.length})`,
    `<table class="test-coverage-table"><tbody>${rows}</tbody></table>`,
  );
}

// ── Helpers ─────────────────────────────────────────────────────────────────

function labeledBlock(label: string, innerHtml: string): string {
  return (
    `<div class="detail-section">` +
      `<div class="detail-section-label">${label}</div>` +
      innerHtml +
    `</div>`
  );
}
