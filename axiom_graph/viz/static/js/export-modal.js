// =============================================================================
// export-modal.ts -- Scope picker for the workflow export
// =============================================================================
import { esc } from './view-utils.js';
// -- State -------------------------------------------------------------------
let _items = [];
let _selected = new Set();
let _search = '';
let _bound = false;
// -- DOM references ----------------------------------------------------------
function _overlay() {
    return document.getElementById('wf-export-overlay');
}
function _listEl() {
    return document.getElementById('wf-export-list');
}
// -- Public API --------------------------------------------------------------
/** Open the picker over the given workflows with an empty selection. */
export function openExportPicker(items) {
    _items = items;
    _selected = new Set();
    _search = '';
    _bindOnce();
    const overlay = _overlay();
    if (!overlay)
        return;
    const searchInput = document.getElementById('wf-export-search');
    if (searchInput)
        searchInput.value = '';
    overlay.classList.remove('hidden');
    _render();
}
/** Close the picker without exporting. */
export function closeExportPicker() {
    const overlay = _overlay();
    if (overlay)
        overlay.classList.add('hidden');
}
// -- Internal ----------------------------------------------------------------
function _bindOnce() {
    if (_bound)
        return;
    _bound = true;
    const overlay = _overlay();
    if (overlay) {
        overlay.addEventListener('click', (e) => { if (e.target === overlay)
            closeExportPicker(); });
    }
    const searchInput = document.getElementById('wf-export-search');
    if (searchInput) {
        searchInput.addEventListener('input', () => {
            _search = searchInput.value.trim().toLowerCase();
            _render();
        });
    }
    const selectAll = document.getElementById('wf-export-select-all');
    if (selectAll) {
        selectAll.addEventListener('click', () => {
            const visible = _visible();
            const allOn = visible.length > 0 && visible.every(i => _selected.has(i.id));
            for (const item of visible) {
                if (allOn)
                    _selected.delete(item.id);
                else
                    _selected.add(item.id);
            }
            _render();
        });
    }
    const runBtn = document.getElementById('wf-export-run');
    if (runBtn)
        runBtn.addEventListener('click', () => _runExport());
    document.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape')
            return;
        const el = _overlay();
        if (el && !el.classList.contains('hidden'))
            closeExportPicker();
    });
    const listEl = _listEl();
    if (listEl) {
        listEl.addEventListener('change', (e) => {
            const cb = e.target.closest('input[type=checkbox]');
            if (!cb)
                return;
            const moduleKey = cb.dataset.module;
            if (moduleKey !== undefined) {
                for (const item of _visible().filter(i => i.module === moduleKey)) {
                    if (cb.checked)
                        _selected.add(item.id);
                    else
                        _selected.delete(item.id);
                }
                _render();
                return;
            }
            const wfId = cb.dataset.wfId;
            if (!wfId)
                return;
            if (cb.checked)
                _selected.add(wfId);
            else
                _selected.delete(wfId);
            _updateCount();
        });
    }
}
function _visible() {
    if (!_search)
        return _items;
    return _items.filter(i => i.name.toLowerCase().includes(_search) || i.module.toLowerCase().includes(_search));
}
function _render() {
    const listEl = _listEl();
    if (!listEl)
        return;
    const groups = new Map();
    for (const item of _visible()) {
        if (!groups.has(item.module))
            groups.set(item.module, []);
        groups.get(item.module).push(item);
    }
    if (groups.size === 0) {
        listEl.innerHTML = '<div class="since-modal-empty">No workflows match.</div>';
        _updateCount();
        return;
    }
    let html = '';
    for (const [modulePath, group] of groups) {
        const allOn = group.every(i => _selected.has(i.id));
        html += `<div class="since-modal-date-separator"><label>` +
            `<input type="checkbox" class="cm-checkbox" data-module="${esc(modulePath)}"${allOn ? ' checked' : ''}>` +
            ` ${esc(modulePath)}</label></div>`;
        for (const item of group) {
            html += `<div class="since-modal-commit-row"><label>` +
                `<input type="checkbox" class="cm-checkbox" data-wf-id="${esc(item.id)}"` +
                `${_selected.has(item.id) ? ' checked' : ''}> ${esc(item.name)}</label></div>`;
        }
    }
    listEl.innerHTML = html;
    _updateCount();
}
function _updateCount() {
    const el = document.getElementById('wf-export-count');
    if (el)
        el.textContent = `${_selected.size} selected`;
}
function _runExport() {
    if (_selected.size === 0)
        return;
    const ids = [..._selected].join(',');
    window.open(`/api/workflow-export?format=html&ids=${encodeURIComponent(ids)}`, '_blank');
    closeExportPicker();
}
