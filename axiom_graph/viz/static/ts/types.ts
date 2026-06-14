// =============================================================================
// types.ts -- Shared interfaces for Cortex Viz frontend
// =============================================================================

/** A cortex index node as returned by /api/all and /api/nodes/{id}. */
export interface AxiomNode {
  id: string;
  title?: string;
  node_type: string;
  subtype?: string;
  status?: string;
  location?: string;
  tags?: string[];
  level_0?: string;
  level_1?: string;
  level_2?: string;
  level_steps?: any[];
  level_3_location?: string;
  dflow_meta?: any;
  // Ghost node fields (from since filter deleted nodes)
  _ghost?: boolean;
  _ghost_change_type?: string;
}

/** An edge between two cortex nodes. */
export interface AxiomEdge {
  id: string;
  from_id: string;
  to_id: string;
  edge_type: string;
  weight?: number;
}

/** Embeddings availability info from /api/meta. */
export interface EmbeddingsInfo {
  available: boolean;
  count: number;
  node_count: number;
  coverage: number;
}

/** Project metadata from /api/meta. */
export interface ProjectMeta {
  project_id: string;
  node_count: number;
  edge_count: number;
  edge_types: string[];
  tags: string[];
  type_counts: Record<string, number>;
  test_paths?: string[];
  embeddings?: EmbeddingsInfo;
}

/** Staleness status string (own dimension). */
export type OwnStalenessStatus =
  | 'VERIFIED'
  | 'CONTENT_UPDATED'
  | 'DESC_UPDATED'
  | 'NOT_FOUND'
  | 'unknown';

/** Staleness status string (link dimension). */
export type LinkStalenessStatus =
  | 'VERIFIED'
  | 'VERIFIED'
  | 'LINKED_STALE'
  | 'BROKEN_LINK'
  | 'unknown';

/** Two-column staleness entry as returned by the API. */
export interface StalenessEntry {
  own_status: string;
  link_status: string;
}

/** Map of node ID to two-column staleness entry. */
export type StalenessMap = Record<string, StalenessEntry>;

/** Extract the primary display staleness from a StalenessEntry or fallback. */
export function displayStaleness(entry: StalenessEntry | undefined): string {
  if (!entry) return 'unknown';
  // Show link status if own is clean/verified and link is stale
  const own = entry.own_status || 'unknown';
  const link = entry.link_status || 'VERIFIED';
  if ((own === 'VERIFIED' || own === 'VERIFIED') && link !== 'VERIFIED' && link !== 'VERIFIED') {
    return link;
  }
  return own;
}

/** Verification record for a node. */
export interface VerificationRecord {
  verified_at?: string;
  verified_by?: string;
  code_hash_at?: string;
  reason?: string;
}

/** A doc section as returned by the API. */
export interface DocSection {
  id: string;
  heading: string;
  content: string;
  depth?: number;
  slug?: string;
  slug_auto?: boolean;
  parent_id?: string;
  tags?: string[];
  links?: DocLink[];
  linked_nodes?: LinkedNode[];
  sections?: RawDocSection[];
}

/** A link reference within a doc section. */
export interface DocLink {
  node_id: string;
  relationship?: string;
}

/** A linked node (resolved from a link). */
export interface LinkedNode {
  node_id: string;
  title?: string;
  node_type?: string;
  relationship?: string;
}

/** Raw doc section as stored in DocJSON on disk. */
export interface RawDocSection {
  heading: string;
  content: string;
  slug?: string;
  slug_auto?: boolean;
  depth?: number;
  id?: string;
  parent_id?: string;
  tags?: string[];
  links?: DocLink[];
  sections?: RawDocSection[];
}

/** A raw DocJSON document as fetched/stored. */
export interface RawDoc {
  title: string;
  tags?: string[];
  sections?: RawDocSection[];
  [key: string]: any;
}

/** Rendered doc data from /api/docs/{id}/rendered. */
export interface RenderedDoc {
  title: string;
  markdown?: string;
  sections: DocSection[];
}

/** Doc list entry from /api/docs. */
export interface DocListEntry {
  id: string;
  title: string;
  source?: string;
  tags?: string[];
  section_count?: number;
}

/** History entry for a node. */
export interface HistoryEntry {
  sha?: string;
  date?: string;
  change_type?: string;
  commit_subject?: string;
}

/** Since filter state. */
export interface SinceFilter {
  type: 'checkpoint' | 'last-commit' | '24h' | '7d' | 'custom' | 'range';
  value?: string | null;
  /** For range mode: the upper bound SHA. */
  untilSha?: string | null;
  /** For range mode: the upper bound timestamp. */
  untilTimestamp?: string | null;
  /** Commit subject for display in the summary card. */
  commitSubject?: string | null;
}

/** A commit entry as returned by /api/history/recent-shas. */
export interface CommitEntry {
  sha: string;
  date: string;
  change_type: string;
  commit_subject: string | null;
  commit_body: string | null;
  is_checkpoint: boolean;
  /** True iff this commit has node_history rows (a valid "since" reference). */
  indexed: boolean;
}

/** Response from /api/history/since. When `resolved` is false the requested
 *  SHA isn't in the index — the viz shows a banner, never a count. */
export interface SinceResponse {
  resolved: boolean;
  node_ids?: string[];
  baseline_sha?: string | null;
  baseline_timestamp?: string | null;
  until_timestamp?: string | null;
  deleted_nodes?: AxiomNode[];
  index_head_sha?: string | null;
  commits_behind_head?: number | null;
  requested_sha?: string | null;
  reason?: string;
}

/** Step edge label data. */
export type StepEdgeLabels = Record<string, string>;

/** Workflow data. */
export interface WorkflowData {
  id: string;
  name: string;
  cortex_node_id?: string;
  module?: string;
  purpose?: string;
  [key: string]: any;
}

/** Staleness cause from /api/nodes/{id}/staleness-cause. */
export interface StalenessCause {
  status: string;
  reasons?: string[];
  linked_stale_nodes?: Array<{
    node_id: string;
    title?: string;
    staleness?: string;
  }>;
  [key: string]: any;
}

/** Filter state for sidebar. */
export interface FilterState {
  types: Set<string>;
  tags: Set<string>;
  status: string;
  staleness: string;
  subtypes: Set<string>;
  edgeTypes: Record<string, boolean>;
  hideOrphans: boolean;
  showPrivate: boolean;
  showTests: boolean;
}

/** App state object. */
export interface AppState {
  view: string;
  meta: ProjectMeta | null;
  allNodes: AxiomNode[];
  allEdges: AxiomEdge[];
  stalenessMap: StalenessMap;
  verificationMap: Record<string, VerificationRecord>;
  filteredNodes: AxiomNode[];
  selectedNodeId: string | null;
  isLargeProject: boolean;
  filters: FilterState;
  tagSearch: string;
  depth: number;
  layout: string;
  stepEdgeLabels: StepEdgeLabels | null;
  dflowWorkflows: WorkflowData[];
  sinceFilter: SinceFilter | null;
  _sinceBaselineSha: string | null;
  _sinceNodeIds: Set<string> | null;
  _sinceDeletedNodes: AxiomNode[] | null;
  _sinceUntilTimestamp: string | null;
  searchMode: 'keyword' | 'semantic';
  searchQuery: string;
  searchResultNodes: AxiomNode[] | null;
}
