// =============================================================================
// doc-tree.ts -- Folder tree + filter panel for docs view
//
// Manages the left sidebar: folder tree, doc list, tag filtering,
// group-by controls, new doc/folder/refresh actions.
// =============================================================================

import type { DocListEntry } from './types.js';
import { esc } from './view-utils.js';
import { getDocs, getKnownSubdirs, docDir, createDoc, createFolder, renameDoc, moveDoc, refreshDocs, fetchDocs, getDocsDirs, getPrimaryDocsDir } from './doc-api.js';

// ── SVG icon constants ──────────────────────────────────────────────────────

const ICON_RENAME = `<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M11.5 1.5l3 3-9 9H2.5v-3l9-9z"/></svg>`;
const ICON_MOVE = `<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 6h12M8 2l4 4-4 4"/></svg>`;
const ICON_FOLDER = `<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 4h5l1.5-2H14v10H2V4z"/></svg>`;
const ICON_DOC = `<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><path d="M4 1.5h5.5L13 5v9.5H4V1.5Z"/><path d="M9.5 1.5V5H13"/></svg>`;

// ── Persistent state (localStorage) ─────────────────────────────────────────

let _expandedFolders: Set<string> | null = null;
let _fTags: Set<string> | null = null;
let _groupBy = 'tree';
let _tagSearch = '';
let _tagSuggestionsLimit = 8;

export function getExpandedFolders(): Set<string> {
  if (!_expandedFolders) {
    try {
      const stored = JSON.parse(localStorage.getItem('cortex-doc-expanded') || '[]');
      _expandedFolders = new Set(stored);
    } catch { _expandedFolders = new Set(); }
  }
  return _expandedFolders;
}

function persistExpandedFolders(): void {
  localStorage.setItem('cortex-doc-expanded', JSON.stringify([...getExpandedFolders()]));
}

export function getFilterTags(): Set<string> {
  if (!_fTags) {
    try {
      const stored = JSON.parse(localStorage.getItem('cortex-doc-tags') || '[]');
      _fTags = new Set(stored);
    } catch { _fTags = new Set(); }
  }
  return _fTags;
}

function persistFilterTags(): void {
  localStorage.setItem('cortex-doc-tags', JSON.stringify([...getFilterTags()]));
}

function toggleFolder(folderPath: string): void {
  const set = getExpandedFolders();
  if (set.has(folderPath)) set.delete(folderPath);
  else set.add(folderPath);
  persistExpandedFolders();
}

// ── Reset ───────────────────────────────────────────────────────────────────

export function resetTree(): void {
  _expandedFolders = null;
  _fTags = null;
}

// ── All tags from docs ──────────────────────────────────────────────────────

/**
 * Return all tags with counts, sorted alphabetically.
 * Each entry is a [tag, count] tuple.
 */
export function allTags(): [string, number][] {
  const counts = new Map<string, number>();
  for (const doc of getDocs()) {
    for (const t of (doc.tags || [])) {
      counts.set(t, (counts.get(t) || 0) + 1);
    }
  }
  return [...counts.entries()].sort((a, b) => a[0].localeCompare(b[0]));
}

// ── Filter logic ────────────────────────────────────────────────────────────

export function applyFilters(docs: DocListEntry[]): DocListEntry[] {
  const fTags = getFilterTags();
  return docs.filter(doc => {
    if (fTags.size > 0) {
      const docTags = new Set(doc.tags || []);
      if (!Array.from(fTags).some(t => docTags.has(t))) return false;
    }
    return true;
  });
}

// ── Folder tree builder ─────────────────────────────────────────────────────

interface TreeNode {
  path: string;
  name: string;
  children: Map<string, TreeNode>;
  docs: DocListEntry[];
}

// Strip the longest configured docs-root prefix from a directory string.
function stripAnyRoot(dir: string, roots: string[]): string {
  let best = '';
  for (const r of roots) {
    const clean = r.replace(/\/+$/, '');
    if (clean && (dir === clean || dir.startsWith(clean + '/')) && clean.length > best.length) {
      best = clean;
    }
  }
  if (!best) return dir;
  return dir.slice(best.length).replace(/^\/+/, '');
}

export function buildFolderTree(docs: DocListEntry[]): TreeNode {
  const primary = getPrimaryDocsDir();
  const allRoots = getDocsDirs();
  const root: TreeNode = { path: primary, name: primary, children: new Map(), docs: [] };

  for (const doc of docs) {
    const dir = docDir(doc);
    const subParts = stripAnyRoot(dir, allRoots).split('/').filter(Boolean);

    if (subParts.length === 0) {
      root.docs.push(doc);
    } else {
      let node = root;
      for (const seg of subParts) {
        const childPath = node.path + '/' + seg;
        if (!node.children.has(seg)) {
          node.children.set(seg, { path: childPath, name: seg, children: new Map(), docs: [] });
        }
        node = node.children.get(seg)!;
      }
      node.docs.push(doc);
    }
  }

  // Merge in known empty subdirectories
  const subdirs = getKnownSubdirs();
  if (subdirs) {
    for (const dir of subdirs) {
      const subParts = stripAnyRoot(dir, allRoots).split('/').filter(Boolean);
      if (subParts.length === 0) continue;
      let node = root;
      for (const seg of subParts) {
        const childPath = node.path + '/' + seg;
        if (!node.children.has(seg)) {
          node.children.set(seg, { path: childPath, name: seg, children: new Map(), docs: [] });
        }
        node = node.children.get(seg)!;
      }
    }
  }

  return root;
}

function countTreeDocs(node: TreeNode): number {
  let count = node.docs.length;
  for (const [, child] of node.children) {
    count += countTreeDocs(child);
  }
  return count;
}

// ── Render folder tree ──────────────────────────────────────────────────────

export function renderFolderTree(
  listEl: HTMLElement,
  filtered: DocListEntry[],
  selectDoc: (docId: string) => void,
  selectedDocId: string | null,
  onRename: (docId: string) => void,
  onMove: (docId: string) => void,
): void {
  const tree = buildFolderTree(filtered);
  listEl.innerHTML = '';
  const expanded = getExpandedFolders();

  renderTreeNode(listEl, tree, 0, expanded, selectDoc, selectedDocId, onRename, onMove);
}

function renderTreeNode(
  container: HTMLElement,
  node: TreeNode,
  depth: number,
  expanded: Set<string>,
  selectDoc: (docId: string) => void,
  selectedDocId: string | null,
  onRename: (docId: string) => void,
  onMove: (docId: string) => void,
): void {
  // For depth > 0, wrap folder + children in a group div for border separators
  let target = container;

  if (depth > 0) {
    const group = document.createElement('div');
    group.className = 'doc-tree-group';
    container.appendChild(group);
    target = group;

    const isExpanded = expanded.has(node.path);
    const count = countTreeDocs(node);
    const el = document.createElement('div');
    el.className = 'doc-tree-folder' + (isExpanded ? ' expanded' : '');
    el.style.paddingLeft = ((depth - 1) * 14 + 8) + 'px';
    el.innerHTML =
      `<span class="doc-tree-toggle">${isExpanded ? '\u25be' : '\u25b8'}</span>` +
      `<span class="doc-tree-icon">${ICON_FOLDER}</span>` +
      `<span class="doc-tree-name">${esc(node.name)}</span>` +
      `<span class="doc-tree-count">${count}</span>` +
      `<span class="doc-tree-action" title="New doc in ${esc(node.path)}">+</span>` +
      `<span class="doc-tree-action doc-tree-mkdir" title="New subfolder in ${esc(node.path)}">${ICON_FOLDER}</span>`;

    el.addEventListener('click', (e) => {
      if ((e.target as HTMLElement).closest('.doc-tree-action')) return;
      toggleFolder(node.path);
      // Re-render the list (caller handles this via full re-render)
      const parent = container.closest('.doc-list');
      if (parent) renderFolderTree(parent as HTMLElement, applyFilters(getDocs()), selectDoc, selectedDocId, onRename, onMove);
    });

    // New doc action
    const newBtn = el.querySelector('.doc-tree-action:not(.doc-tree-mkdir)');
    if (newBtn) {
      newBtn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const title = prompt('New document title:');
        if (!title || !title.trim()) return;
        try {
          const data = await createDoc(title.trim(), node.path);
          await fetchDocs();
          if (data.doc_id) selectDoc(data.doc_id);
        } catch (err: any) {
          alert('Error creating document: ' + err.message);
        }
      });
    }

    // Mkdir action
    const mkdirBtn = el.querySelector('.doc-tree-mkdir');
    if (mkdirBtn) {
      mkdirBtn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const name = prompt('Subfolder name:');
        if (!name || !name.trim()) return;
        try {
          await createFolder(node.path, name.trim());
          await fetchDocs();
          const parent = container.closest('.doc-list');
          if (parent) renderFolderTree(parent as HTMLElement, applyFilters(getDocs()), selectDoc, selectedDocId, onRename, onMove);
        } catch (err: any) {
          alert('Error creating folder: ' + err.message);
        }
      });
    }

    target.appendChild(el);

    if (!isExpanded) return;
  }

  // Sort children then render
  const sortedChildren = [...node.children.entries()].sort(([a], [b]) => a.localeCompare(b));
  for (const [, child] of sortedChildren) {
    renderTreeNode(target, child, depth + 1, expanded, selectDoc, selectedDocId, onRename, onMove);
  }

  // Render docs in this folder
  for (const doc of node.docs.sort((a, b) => (a.title || '').localeCompare(b.title || ''))) {
    const item = buildDocListItem(doc, selectDoc, selectedDocId, onRename, onMove, depth);
    target.appendChild(item);
  }
}

function buildDocListItem(
  doc: DocListEntry,
  selectDoc: (docId: string) => void,
  selectedDocId: string | null,
  onRename: (docId: string) => void,
  onMove: (docId: string) => void,
  depth: number,
): HTMLElement {
  const el = document.createElement('div');
  el.className = 'doc-list-item' + (doc.id === selectedDocId ? ' active' : '');
  el.style.paddingLeft = (depth * 14 + 8) + 'px';

  const tagChips = (doc.tags || []).slice(0, 3)
    .map(t => `<span class="doc-list-tag">${esc(t)}</span>`).join('');

  el.innerHTML =
    `<div class="doc-list-title">` +
      `<span class="doc-item-icon">${ICON_DOC}</span>` +
      `<span class="doc-item-name">${esc(doc.title)}</span>` +
      `<span class="doc-item-actions">` +
        `<button class="doc-item-action" data-action="rename" title="Rename">${ICON_RENAME}</button>` +
        `<button class="doc-item-action" data-action="move" title="Move">${ICON_MOVE}</button>` +
      `</span>` +
    `</div>` +
    `<div class="doc-list-meta"><span class="doc-list-sections">${doc.section_count || 0} sections</span>${tagChips}</div>`;

  el.addEventListener('click', (e) => {
    if ((e.target as HTMLElement).closest('.doc-item-action')) return;
    selectDoc(doc.id);
  });

  const renameBtn = el.querySelector('[data-action="rename"]');
  if (renameBtn) renameBtn.addEventListener('click', (e) => { e.stopPropagation(); onRename(doc.id); });
  const moveBtn = el.querySelector('[data-action="move"]');
  if (moveBtn) moveBtn.addEventListener('click', (e) => { e.stopPropagation(); onMove(doc.id); });

  return el;
}

// ── Filter panel builder ────────────────────────────────────────────────────

export function buildFilterPanel(
  panel: HTMLElement,
  onFilterChange: () => void,
): void {
  const tagTuples = allTags();
  const fTags = getFilterTags();
  const hasFilters = fTags.size > 0;

  // Selected tag chips with counts
  const chipsHtml = [...fTags].map(t => {
    const count = tagTuples.find(([k]) => k === t)?.[1] || 0;
    return `<span class="doc-fp-chip" data-tag="${esc(t)}" title="${esc(t)}">` +
      `<span class="doc-fp-chip-label">${esc(t)}</span>` +
      `<span class="doc-fp-chip-count">${count}</span>` +
      `<span class="doc-fp-chip-x">&times;</span>` +
    `</span>`;
  }).join('');

  // Tags sorted by count (descending), excluding already-selected
  const tagsByCount = [...tagTuples].sort((a, b) => b[1] - a[1]);
  const unselected = tagsByCount.filter(([t]) => !fTags.has(t));
  const searchLower = _tagSearch.toLowerCase();
  const matchingTags = searchLower
    ? unselected.filter(([t]) => t.toLowerCase().includes(searchLower))
    : unselected;
  const visibleTags = matchingTags.slice(0, _tagSuggestionsLimit);
  const hiddenCount = matchingTags.length - visibleTags.length;

  const suggestionsHtml = visibleTags.map(([t, count]) => `
    <div class="doc-fp-suggestion" data-tag="${esc(t)}" title="${esc(t)}">
      <span class="doc-fp-suggestion-name">${esc(t)}</span>
      <span class="doc-fp-count">${count}</span>
    </div>`).join('');

  panel.innerHTML = `
    <div class="doc-fp-section">
      <div class="doc-fp-heading">Tags (${tagTuples.length})</div>
      ${chipsHtml ? `<div class="doc-fp-chips" id="doc-fp-chips">${chipsHtml}</div>` : ''}
      <input type="text" class="doc-fp-search" id="doc-tag-search"
             placeholder="Search tags\u2026" value="${esc(_tagSearch)}">
      <div class="doc-fp-suggestions" id="doc-fp-suggestions">
        ${suggestionsHtml}
      </div>
      ${hiddenCount > 0
        ? `<button class="doc-fp-show-all" id="doc-fp-show-all">Show all (+${hiddenCount} more)</button>`
        : ''}
    </div>
    ${hasFilters
      ? '<button class="doc-fp-clear" id="doc-fp-clear">Clear filters</button>'
      : ''}`;

  // Wire events

  // Chip click -- remove tag from filter
  panel.querySelectorAll('.doc-fp-chip').forEach(el => {
    el.addEventListener('click', () => {
      fTags.delete((el as HTMLElement).dataset.tag || '');
      persistFilterTags();
      onFilterChange();
      buildFilterPanel(panel, onFilterChange);
    });
  });

  // Suggestion rows -- add tag to filter
  panel.querySelectorAll('.doc-fp-suggestion').forEach(el => {
    el.addEventListener('click', () => {
      fTags.add((el as HTMLElement).dataset.tag || '');
      persistFilterTags();
      _tagSearch = '';
      onFilterChange();
      buildFilterPanel(panel, onFilterChange);
    });
  });

  // "Show all" button -- reveal the rest
  const showAllBtn = panel.querySelector('#doc-fp-show-all');
  if (showAllBtn) {
    showAllBtn.addEventListener('click', () => {
      _tagSuggestionsLimit = matchingTags.length;
      buildFilterPanel(panel, onFilterChange);
    });
  }

  // Tag search input
  const tagSearchInput = panel.querySelector('#doc-tag-search') as HTMLInputElement | null;
  if (tagSearchInput) {
    tagSearchInput.addEventListener('input', () => {
      _tagSearch = tagSearchInput.value;
      _tagSuggestionsLimit = 8;
      buildFilterPanel(panel, onFilterChange);
    });
    // Keep focus after rebuild
    tagSearchInput.focus();
    tagSearchInput.setSelectionRange(tagSearchInput.value.length, tagSearchInput.value.length);
  }

  // Clear button
  const clearBtn = panel.querySelector('#doc-fp-clear');
  if (clearBtn) {
    clearBtn.addEventListener('click', () => {
      fTags.clear();
      persistFilterTags();
      _tagSearch = '';
      _tagSuggestionsLimit = 8;
      buildFilterPanel(panel, onFilterChange);
      onFilterChange();
    });
  }
}
