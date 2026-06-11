// =============================================================================
// graph.ts -- Cytoscape.js graph renderer
// =============================================================================

import type { AxiomNode, AxiomEdge, StalenessMap, StepEdgeLabels } from './types.js';
import { displayStaleness } from './types.js';

// Cytoscape is loaded via CDN <script> tag (not ESM)
declare const cytoscape: any;
declare const cytoscapeDagre: any;

// ── Colour maps ─────────────────────────────────────────────────────────────

export const NODE_TYPE_COLORS: Record<string, string> = {
  composite_process: '#4a90d9',
  atomic_process:    '#27ae60',
  entity:            '#9b59b6',
};

export const SUBTYPE_COLORS: Record<string, string> = {
  module:           '#4a90d9',
  function:         '#27ae60',
  test:             '#e74c3c',
  docjson:          '#f39c12',
  config:           '#f0883e',
  external_package: '#7f8c8d',
};

export const NODE_TYPE_DEFAULT = '#7f8c8d';

export const EDGE_TYPE_COLORS: Record<string, string> = {
  composes:      '#4a90d9',
  delegates_to:  '#f39c12',
  depends_on:    '#adb5bd',
  documents:     '#27ae60',
  references:    '#e67e22',
  calls:         '#e74c3c',
  inherits:      '#8e44ad',
};

export const EDGE_DEFAULT = '#adb5bd';

// ── Internal state ──────────────────────────────────────────────────────────

let cy: any = null;

/** Access the underlying Cytoscape instance (for external modules). */
export function getCy(): any {
  return cy;
}

// ── Cytoscape stylesheet ────────────────────────────────────────────────────

function buildStyle(): any[] {
  return [
    {
      selector: 'node',
      style: {
        'background-color': (ele: any) => SUBTYPE_COLORS[ele.data('subtype')] || NODE_TYPE_COLORS[ele.data('node_type')] || NODE_TYPE_DEFAULT,
        'label':            'data(title)',
        'font-size':        '10px',
        'text-valign':      'bottom',
        'text-halign':      'center',
        'width':             26,
        'height':            26,
        'color':            '#333',
        'text-outline-width': 2,
        'text-outline-color': '#fff',
        'text-max-width':   '120px',
        'text-wrap':        'ellipsis',
        'border-width':      2,
        'border-color':     '#fff',
      },
    },
    {
      selector: 'node.hub',
      style: { width: 36, height: 36, 'font-size': '11px', 'font-weight': 'bold' },
    },
    {
      selector: 'node:selected',
      style: {
        'border-width': 3,
        'border-color': '#ff6b35',
        'overlay-color': '#ff6b35',
        'overlay-padding': 4,
        'overlay-opacity': 0.15,
      },
    },
    // Staleness rings — own dimension (new names)
    { selector: 'node[staleness="VERIFIED"]',         style: { 'border-color': '#34d058', 'border-width': 2 } },
    { selector: 'node[staleness="CONTENT_UPDATED"]',  style: { 'border-color': '#f39c12', 'border-width': 3 } },
    { selector: 'node[staleness="DESC_UPDATED"]',     style: { 'border-color': '#c9a227', 'border-width': 2 } },
    { selector: 'node[staleness="NOT_FOUND"]',        style: { 'border-color': '#e74c3c', 'border-width': 3 } },
    // Staleness rings — link dimension
    { selector: 'node[staleness="LINKED_STALE"]',     style: { 'border-color': '#e38a41', 'border-width': 3 } },
    { selector: 'node[staleness="BROKEN_LINK"]',      style: { 'border-color': '#e74c3c', 'border-width': 3 } },
    // Dimmed (filtered out / not in search results)
    { selector: 'node.dimmed', style: { opacity: 0.15 } },
    {
      selector: 'edge',
      style: {
        'line-color':          (ele: any) => EDGE_TYPE_COLORS[ele.data('edge_type')] || EDGE_DEFAULT,
        'target-arrow-color':  (ele: any) => EDGE_TYPE_COLORS[ele.data('edge_type')] || EDGE_DEFAULT,
        'target-arrow-shape':  'triangle',
        'curve-style':         'bezier',
        'width':                1.5,
        'opacity':              0.65,
      },
    },
    { selector: 'edge:selected', style: { width: 3, opacity: 1 } },
    // Step-number labels on delegates_to edges (from workflow annotations)
    {
      selector: 'edge[step_label != ""]',
      style: {
        'label':                   'data(step_label)',
        'font-size':               '9px',
        'text-rotation':           'autorotate',
        'text-margin-y':           -8,
        'color':                   '#e67e22',
        'text-background-color':   '#fff',
        'text-background-opacity': 0.85,
        'text-background-padding': '2px',
      },
    },
  ];
}

// ── Layouts ──────────────────────────────────────────────────────────────────

function layoutOptions(name: string, callback?: () => void): any {
  const opts: Record<string, any> = {
    cose: {
      name: 'cose',
      animate: false,
      idealEdgeLength: 80,
      nodeRepulsion: 450000,
      nodeOverlap: 20,
      gravity: 90,
      numIter: 800,
      stop: callback,
    },
    dagre: {
      name: 'dagre',
      rankDir: 'TB',
      nodeSep: 50,
      rankSep: 80,
      animate: false,
      stop: callback,
    },
    breadthfirst: {
      name: 'breadthfirst',
      directed: true,
      animate: false,
      spacingFactor: 1.5,
      stop: callback,
    },
    grid: {
      name: 'grid',
      animate: false,
      stop: callback,
    },
    concentric: {
      name: 'concentric',
      animate: false,
      concentric: (node: any) => node.degree(),
      levelWidth: () => 2,
      stop: callback,
    },
  };
  return opts[name] || opts.cose;
}

// ── Element builders ────────────────────────────────────────────────────────

function nodeEls(nodes: AxiomNode[], stalenessMap: StalenessMap): any[] {
  return nodes.map(n => ({
    data: {
      id:        n.id,
      title:     n.title || n.id.split('::').pop(),
      node_type: n.node_type,
      subtype:   n.subtype || '',
      status:    n.status,
      staleness: displayStaleness(stalenessMap[n.id]),
      location:  n.location,
    },
  }));
}

function edgeEls(edges: AxiomEdge[], stepLabels: StepEdgeLabels | null): any[] {
  return edges.map(e => {
    const labelKey = `${e.from_id}|${e.to_id}`;
    const stepLabel = (e.edge_type === 'delegates_to' && stepLabels)
      ? (stepLabels[labelKey] || '')
      : '';
    return {
      data: {
        id:         e.id,
        source:     e.from_id,
        target:     e.to_id,
        edge_type:  e.edge_type,
        weight:     e.weight,
        step_label: stepLabel,
      },
    };
  });
}

// ── Init ────────────────────────────────────────────────────────────────────

/** Bind internal tap events. Needs selectNodeFn and closeDetailFn callbacks
 *  to avoid circular import with app.ts. */
let onNodeTap: ((nodeId: string) => void) | null = null;
let onBackgroundTap: (() => void) | null = null;

export function setEventHandlers(
  selectNode: (nodeId: string) => void,
  closeDetail: () => void,
): void {
  onNodeTap = selectNode;
  onBackgroundTap = closeDetail;
}

function bindEvents(): void {
  if (!cy) return;
  cy.on('tap', 'node', (evt: any) => {
    if (onNodeTap) onNodeTap(evt.target.data('id'));
  });
  cy.on('tap', (evt: any) => {
    if (evt.target === cy && onBackgroundTap) onBackgroundTap();
  });
}

export function init(container: HTMLElement): void {
  if (cy) { cy.destroy(); cy = null; }

  // Register the dagre layout plugin if the library is present
  if (typeof cytoscapeDagre !== 'undefined') {
    try { cytoscape.use(cytoscapeDagre); } catch (_) { /* already registered */ }
  }

  cy = cytoscape({
    container,
    style: buildStyle(),
    elements: [],
    minZoom: 0.04,
    maxZoom: 5,
    wheelSensitivity: 0.3,
  });

  bindEvents();
}

// ── Load helpers ────────────────────────────────────────────────────────────

export function loadAll(
  nodes: AxiomNode[],
  edges: AxiomEdge[],
  stalenessMap: StalenessMap,
  layoutName = 'cose',
  stepLabels: StepEdgeLabels | null = null,
): void {
  if (!cy) return;

  const nodeIds = new Set(nodes.map(n => n.id));
  const validEdges = edges.filter(e => nodeIds.has(e.from_id) && nodeIds.has(e.to_id));

  // Compute in-degree to identify hubs
  const inDegree: Record<string, number> = {};
  for (const e of validEdges) {
    inDegree[e.to_id] = (inDegree[e.to_id] || 0) + 1;
  }

  cy.elements().remove();
  cy.add(nodeEls(nodes, stalenessMap));
  cy.add(edgeEls(validEdges, stepLabels));

  // Mark high in-degree nodes
  cy.nodes().forEach((n: any) => {
    if ((inDegree[n.data('id')] || 0) > 3) n.addClass('hub');
  });

  cy.layout(layoutOptions(layoutName)).run();
}

export function loadNeighborhood(
  nodes: AxiomNode[],
  edges: AxiomEdge[],
  centerId: string,
  stalenessMap: StalenessMap,
  layoutName = 'cose',
  stepLabels: StepEdgeLabels | null = null,
): void {
  if (!cy) return;

  cy.elements().remove();
  cy.add(nodeEls(nodes, stalenessMap));
  cy.add(edgeEls(edges, stepLabels));

  cy.layout(layoutOptions(layoutName, () => {
    const center = cy.getElementById(centerId);
    if (center.length) {
      cy.animate({
        fit: { eles: center.neighbourhood().add(center), padding: 80 },
        duration: 400,
      });
      center.select();
    }
  })).run();
}

// ── Layout ──────────────────────────────────────────────────────────────────

export function setLayout(name: string): void {
  if (cy && cy.nodes().length > 0) {
    cy.layout(layoutOptions(name)).run();
  }
}

// ── Selection / highlighting ────────────────────────────────────────────────

export function selectNode(nodeId: string): void {
  if (!cy) return;
  cy.nodes().unselect();
  const node = cy.getElementById(nodeId);
  if (node.length) {
    node.select();
    cy.animate({ center: { eles: node }, zoom: Math.max(cy.zoom(), 1.2), duration: 300 });
  }
}

export function deselectAll(): void {
  if (cy) cy.nodes().unselect();
}

export function filterNodes(visibleIds: Set<string>): void {
  if (!cy) return;
  cy.nodes().forEach((n: any) => {
    const visible = visibleIds.has(n.data('id'));
    n.style('display', visible ? 'element' : 'none');
  });
  cy.edges().forEach((e: any) => {
    const ok = visibleIds.has(e.data('source')) && visibleIds.has(e.data('target'));
    e.style('display', ok ? 'element' : 'none');
  });
}

export function highlightNodes(matchIds: Set<string>): void {
  if (!cy) return;
  cy.nodes().forEach((n: any) => {
    if (matchIds.has(n.data('id'))) n.removeClass('dimmed');
    else                            n.addClass('dimmed');
  });
}

export function clearHighlights(): void {
  if (cy) cy.nodes().removeClass('dimmed');
}

export function applyEdgeFilter(edgeTypes: Record<string, boolean>): void {
  if (!cy) return;
  cy.edges().forEach((e: any) => {
    const et = e.data('edge_type');
    if (edgeTypes[et] === false) {
      e.style('display', 'none');
    } else {
      const srcHidden = e.source().style('display') === 'none';
      const tgtHidden = e.target().style('display') === 'none';
      if (!srcHidden && !tgtHidden) e.style('display', 'element');
    }
  });
}

export function hideOrphanNodes(baseVisibleIds: Set<string>): void {
  if (!cy) return;
  cy.nodes().forEach((n: any) => {
    const nid = n.data('id');
    if (!baseVisibleIds.has(nid)) return;
    const hasVisibleEdge = n.connectedEdges().some(
      (e: any) => e.style('display') !== 'none' && !e.hasClass('hidden-type'),
    );
    n.style('display', hasVisibleEdge ? 'element' : 'none');
  });
}
