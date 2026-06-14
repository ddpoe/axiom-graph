// =============================================================================
// since-modal.ts -- Commit picker modal for since-filter range/browse selection
// =============================================================================

import type { CommitEntry } from './types.js';
import { esc, apiFetch } from './view-utils.js';

// ── Types ────────────────────────────────────────────────────────────────────

/** Result of a single-commit selection. */
export interface SingleSelection {
  mode: 'single';
  sha: string;
  commitSubject: string | null;
}

/** Result of a two-commit range selection. */
export interface RangeSelection {
  mode: 'range';
  sinceSha: string;
  untilSha: string;
  sinceSubject: string | null;
  untilSubject: string | null;
}

export type ModalSelection = SingleSelection | RangeSelection;

/** Callbacks provided by app.ts during init. */
export interface SinceModalCallbacks {
  onSelect: (selection: ModalSelection) => void;
}

// ── State ────────────────────────────────────────────────────────────────────

let _callbacks: SinceModalCallbacks | null = null;
let _allCommits: CommitEntry[] = [];
let _checkedShas: string[] = [];  // 0, 1, or 2 SHAs
let _expandedShas: Set<string> = new Set();
let _searchTerm = '';
let _dateFilter = '';
let _debounceTimer: ReturnType<typeof setTimeout> | null = null;

// ── DOM References ───────────────────────────────────────────────────────────

function _overlay(): HTMLElement | null {
  return document.getElementById('since-modal-overlay');
}
function _listEl(): HTMLElement | null {
  return document.getElementById('since-modal-commit-list');
}
function _rangeBar(): HTMLElement | null {
  return document.getElementById('since-modal-range-bar');
}
function _rangeLabel(): HTMLElement | null {
  return document.getElementById('since-modal-range-label');
}

// ── Public API ───────────────────────────────────────────────────────────────

/** Initialize the modal with callbacks. Called once from app.ts init(). */
export function initSinceModal(callbacks: SinceModalCallbacks): void {
  _callbacks = callbacks;

  // Bind static event listeners
  const overlay = _overlay();
  if (overlay) {
    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) _close();
    });
  }

  const searchInput = document.getElementById('since-modal-search') as HTMLInputElement | null;
  if (searchInput) {
    searchInput.addEventListener('input', () => {
      if (_debounceTimer) clearTimeout(_debounceTimer);
      _debounceTimer = setTimeout(() => {
        _searchTerm = searchInput.value.trim().toLowerCase();
        _renderCommitList();
      }, 200);
    });
  }

  const dateInput = document.getElementById('since-modal-date') as HTMLInputElement | null;
  if (dateInput) {
    dateInput.addEventListener('input', () => {
      _dateFilter = dateInput.value.trim();
      _renderCommitList();
    });
  }

  const applyRangeBtn = document.getElementById('since-modal-apply-range');
  if (applyRangeBtn) {
    applyRangeBtn.addEventListener('click', _applyRange);
  }

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      const overlay = _overlay();
      if (overlay && !overlay.classList.contains('hidden')) {
        _close();
      }
    }
  });
}

/** Open the commit picker modal. Fetches fresh data from the API. */
export async function openCommitPicker(): Promise<void> {
  const overlay = _overlay();
  if (!overlay) return;

  // Reset state
  _checkedShas = [];
  _expandedShas.clear();
  _searchTerm = '';
  _dateFilter = '';

  const searchInput = document.getElementById('since-modal-search') as HTMLInputElement | null;
  if (searchInput) searchInput.value = '';
  const dateInput = document.getElementById('since-modal-date') as HTMLInputElement | null;
  if (dateInput) dateInput.value = '';

  // Show modal with loading state
  overlay.classList.remove('hidden');
  const listEl = _listEl();
  if (listEl) {
    listEl.innerHTML = '<div class="since-modal-empty">Loading commits...</div>';
  }
  _updateRangeBar();

  // Fetch commit data
  try {
    const data = await apiFetch<{ shas: CommitEntry[] }>('/api/history/recent-shas');
    _allCommits = data.shas || [];
    _renderCommitList();
  } catch {
    if (listEl) {
      listEl.innerHTML = '<div class="since-modal-empty">Failed to load commits.</div>';
    }
  }

  // Focus search
  if (searchInput) searchInput.focus();
}

/** Close the modal without making a selection. */
export function closeModal(): void {
  _close();
}

// ── Internal ─────────────────────────────────────────────────────────────────

function _close(): void {
  const overlay = _overlay();
  if (overlay) overlay.classList.add('hidden');
}

/** Filter commits by search term and date. */
function _filteredCommits(): CommitEntry[] {
  let commits = _allCommits;
  if (_searchTerm) {
    commits = commits.filter(c => {
      const text = [c.sha, c.commit_subject || '', c.commit_body || ''].join(' ').toLowerCase();
      return text.includes(_searchTerm);
    });
  }
  if (_dateFilter) {
    commits = commits.filter(c => {
      const commitDate = (c.date || '').slice(0, 10);
      return commitDate >= _dateFilter;
    });
  }
  return commits;
}

/** Render the commit list with date separators. */
function _renderCommitList(): void {
  const listEl = _listEl();
  if (!listEl) return;

  const commits = _filteredCommits();

  if (commits.length === 0) {
    const term = _searchTerm || _dateFilter;
    listEl.innerHTML = `<div class="since-modal-empty">No commits matching '${esc(term)}'</div>`;
    _updateHint(0);
    return;
  }

  listEl.innerHTML = '';
  let lastDate = '';

  // Determine in-range SHAs for highlighting
  const rangeSet = _getRangeSet(commits);

  for (const commit of commits) {
    const commitDate = (commit.date || '').slice(0, 10);
    if (commitDate !== lastDate) {
      lastDate = commitDate;
      const sep = document.createElement('div');
      sep.className = 'since-modal-date-separator';
      sep.textContent = _formatDateSeparator(commitDate);
      listEl.appendChild(sep);
    }

    const row = _createCommitRow(commit, rangeSet);
    listEl.appendChild(row);
  }

  _updateHint(commits.length);
}

/** Create a single commit row element. */
function _createCommitRow(commit: CommitEntry, rangeSet: Set<string>): HTMLElement {
  const row = document.createElement('div');
  row.className = 'since-modal-commit-row';
  if (rangeSet.has(commit.sha)) {
    row.classList.add('in-range');
  }
  // Un-indexed commits have no node_history rows, so they can't be a valid
  // "since" reference — fade them out and make them non-selectable.
  if (!commit.indexed) {
    row.classList.add('not-indexed');
    row.title = 'Not in the index — rebuild (axiom-graph build) to filter since this commit';
  }

  // Checkbox
  const checkbox = document.createElement('input');
  checkbox.type = 'checkbox';
  checkbox.className = 'cm-checkbox';
  checkbox.checked = _checkedShas.includes(commit.sha);
  checkbox.disabled = !commit.indexed;
  checkbox.addEventListener('click', (e) => {
    e.stopPropagation();
    if (!commit.indexed) return;
    _toggleCheck(commit.sha);
  });
  row.appendChild(checkbox);

  // SHA
  const shaEl = document.createElement('span');
  shaEl.className = 'since-modal-commit-sha';
  const shortSha = commit.sha.slice(0, 8);
  shaEl.innerHTML = _searchTerm ? _highlight(shortSha, _searchTerm) : esc(shortSha);
  row.appendChild(shaEl);

  // Badge
  if (commit.is_checkpoint) {
    const badge = document.createElement('span');
    badge.className = 'since-modal-commit-badge checkpoint';
    badge.textContent = 'CHECKPOINT';
    row.appendChild(badge);
  } else if (commit.change_type && commit.change_type !== 'CONTENT_ONLY' && commit.change_type !== 'DESC_ONLY') {
    const badge = document.createElement('span');
    badge.className = 'since-modal-commit-badge build';
    badge.textContent = commit.change_type;
    row.appendChild(badge);
  }

  // Info container (subject + expand + body)
  const info = document.createElement('div');
  info.className = 'since-modal-commit-info';

  const subject = commit.commit_subject || commit.change_type || '';
  const subjectEl = document.createElement('div');
  subjectEl.className = 'since-modal-commit-subject';
  subjectEl.innerHTML = _searchTerm ? _highlight(subject, _searchTerm) : esc(subject);
  info.appendChild(subjectEl);

  // Expand toggle for body
  if (commit.commit_body) {
    const expandEl = document.createElement('span');
    expandEl.className = 'since-modal-commit-expand';
    const isExpanded = _expandedShas.has(commit.sha);
    expandEl.textContent = isExpanded ? 'Hide body' : 'Show body...';
    expandEl.addEventListener('click', (e) => {
      e.stopPropagation();
      if (_expandedShas.has(commit.sha)) {
        _expandedShas.delete(commit.sha);
      } else {
        _expandedShas.add(commit.sha);
      }
      _renderCommitList();
    });
    info.appendChild(expandEl);

    if (isExpanded) {
      const bodyEl = document.createElement('div');
      bodyEl.className = 'since-modal-commit-body';
      bodyEl.textContent = commit.commit_body;
      info.appendChild(bodyEl);
    }
  }

  row.appendChild(info);

  // Date (right-aligned)
  const dateEl = document.createElement('span');
  dateEl.className = 'since-modal-commit-date';
  dateEl.textContent = _formatTime(commit.date);
  row.appendChild(dateEl);

  // Row click = single select
  row.addEventListener('click', (e) => {
    // Don't trigger if clicking checkbox or expand
    const target = e.target as HTMLElement;
    if (target.tagName === 'INPUT' || target.classList.contains('since-modal-commit-expand')) return;
    if (!commit.indexed) return;  // un-indexed: not a valid reference point
    _selectSingle(commit);
  });

  return row;
}

/** Handle checking/unchecking a commit for range selection. */
function _toggleCheck(sha: string): void {
  const idx = _checkedShas.indexOf(sha);
  if (idx >= 0) {
    // Uncheck
    _checkedShas.splice(idx, 1);
  } else if (_checkedShas.length < 2) {
    // Check (up to 2)
    _checkedShas.push(sha);
  } else {
    // Already 2 checked: replace the furthest one
    // "Furthest" = the one that would expand the range most.
    // Simple approach: replace the first checked.
    _checkedShas.shift();
    _checkedShas.push(sha);
  }
  _updateRangeBar();
  _renderCommitList();
}

/** Compute which SHAs are in the selected range (inclusive). */
function _getRangeSet(commits: CommitEntry[]): Set<string> {
  const result = new Set<string>();
  if (_checkedShas.length !== 2) return result;

  const indices = _checkedShas.map(sha =>
    commits.findIndex(c => c.sha === sha)
  ).filter(i => i >= 0).sort((a, b) => a - b);

  if (indices.length !== 2) return result;

  for (let i = indices[0]; i <= indices[1]; i++) {
    result.add(commits[i].sha);
  }
  return result;
}

/** Update the range bar visibility and content. */
function _updateRangeBar(): void {
  const bar = _rangeBar();
  const label = _rangeLabel();
  if (!bar || !label) return;

  if (_checkedShas.length === 2) {
    // Determine which is older/newer based on position in _allCommits
    const idx0 = _allCommits.findIndex(c => c.sha === _checkedShas[0]);
    const idx1 = _allCommits.findIndex(c => c.sha === _checkedShas[1]);
    // _allCommits is newest-first, so higher index = older
    const olderSha = idx0 > idx1 ? _checkedShas[0] : _checkedShas[1];
    const newerSha = idx0 > idx1 ? _checkedShas[1] : _checkedShas[0];

    const rangeCount = Math.abs(idx1 - idx0) + 1;
    label.innerHTML =
      `<span>${esc(olderSha.slice(0, 8))}</span>` +
      ` &rarr; ` +
      `<span>${esc(newerSha.slice(0, 8))}</span>` +
      ` <span style="color:var(--text-secondary)">(${rangeCount} commits)</span>`;
    bar.classList.remove('hidden');
  } else {
    bar.classList.add('hidden');
  }
}

/** Apply a single-commit selection. */
function _selectSingle(commit: CommitEntry): void {
  if (!_callbacks) return;
  _callbacks.onSelect({
    mode: 'single',
    sha: commit.sha,
    commitSubject: commit.commit_subject,
  });
  _close();
}

/** Apply the checked range selection. */
function _applyRange(): void {
  if (!_callbacks || _checkedShas.length !== 2) return;

  // Determine older/newer
  const idx0 = _allCommits.findIndex(c => c.sha === _checkedShas[0]);
  const idx1 = _allCommits.findIndex(c => c.sha === _checkedShas[1]);
  // _allCommits is newest-first, so higher index = older
  const olderIdx = Math.max(idx0, idx1);
  const newerIdx = Math.min(idx0, idx1);

  const sinceSha = _allCommits[olderIdx].sha;
  const untilSha = _allCommits[newerIdx].sha;

  _callbacks.onSelect({
    mode: 'range',
    sinceSha,
    untilSha,
    sinceSubject: _allCommits[olderIdx].commit_subject,
    untilSubject: _allCommits[newerIdx].commit_subject,
  });
  _close();
}

/** Format a date string for the separator (e.g., "Apr 2, 2026"). */
function _formatDateSeparator(dateStr: string): string {
  try {
    const d = new Date(dateStr + 'T00:00:00');
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
  } catch {
    return dateStr;
  }
}

/** Format a timestamp to show just the time portion. */
function _formatTime(dateStr: string): string {
  try {
    const d = new Date(dateStr);
    return d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' });
  } catch {
    return dateStr?.slice(11, 16) || '';
  }
}

/** Highlight search matches in text. */
function _highlight(text: string, term: string): string {
  if (!term) return esc(text);
  const escaped = esc(text);
  const termEscaped = esc(term);
  // Case-insensitive replace in the escaped HTML
  const regex = new RegExp(`(${termEscaped.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`, 'gi');
  return escaped.replace(regex, '<mark>$1</mark>');
}

/** Update the hint bar with match count. */
function _updateHint(count: number): void {
  const hint = document.querySelector('.since-modal-hint');
  if (!hint) return;
  if (_searchTerm || _dateFilter) {
    (hint as HTMLElement).textContent = `${count} commit${count !== 1 ? 's' : ''} matching. Click a row = filter since that commit. Check two = select a range. Esc to close.`;
  } else {
    (hint as HTMLElement).textContent = 'Click a row = filter since that commit. Check two = select a range. Esc to close.';
  }
}
