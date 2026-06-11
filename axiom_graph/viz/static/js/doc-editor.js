// =============================================================================
// doc-editor.ts -- Section rendering + Tiptap lifecycle + section CRUD
//
// Handles the main content area of the docs view: rendering sections,
// managing Tiptap editors, inline heading/slug editing, link search,
// section add/move/remove, TOC, and scroll tracking.
// =============================================================================
import { esc, apiFetch } from './view-utils.js';
import { getRawDoc, setRawDoc, isDirty, setDirty, getSelectedDocId, saveDoc, getPendingRenames, clearPendingRenames, addPendingRename, destroyRawMonaco, setRawMonaco, getRawMonaco, } from './doc-api.js';
import { isMermaidSection, extractMermaidSource, runMermaid as runMermaidDiagrams, setMermaidSources, clearMermaidSources, getDiagramEditorIdx, openDiagramEditor, closeDiagramEditor, applyDiagramEditor, mountDiagramEditor, destroyDiagramEditor, } from './doc-diagrams.js';
// ── State ───────────────────────────────────────────────────────────────────
let _renderedData = null;
let _editingSectionIdx = null;
let _editingHeadingIdx = null;
let _editingSlugIdx = null;
let _linkSearchIdx = null;
let _linkSearchResults = [];
let _linkSearchTimer = null;
let _tiptapEditors = {};
let _viewRaw = false;
let _collapsedSections = null;
let _expandedSubsections = null;
let _scrollObserver = null;
// ── Getters ─────────────────────────────────────────────────────────────────
export function getRenderedData() { return _renderedData; }
export function setRenderedData(data) { _renderedData = data; }
export function isViewRaw() { return _viewRaw; }
export function setViewRaw(raw) { _viewRaw = raw; }
// ── Reset ───────────────────────────────────────────────────────────────────
export function resetEditor() {
    _renderedData = null;
    _editingSectionIdx = null;
    _editingHeadingIdx = null;
    _editingSlugIdx = null;
    _viewRaw = false;
    _collapsedSections = null;
    _expandedSubsections = null;
    destroyEditors();
    destroyScrollObserver();
    destroyRawMonaco();
}
// ── Section collapse management ─────────────────────────────────────────────
function getCollapsedSections() {
    if (!_collapsedSections) {
        try {
            const stored = JSON.parse(sessionStorage.getItem('cortex-doc-collapsed') || '[]');
            _collapsedSections = new Set(stored);
        }
        catch {
            _collapsedSections = new Set();
        }
    }
    return _collapsedSections;
}
function persistCollapsedSections() {
    sessionStorage.setItem('cortex-doc-collapsed', JSON.stringify([...getCollapsedSections()]));
}
function toggleSectionCollapse(sectionId) {
    const set = getCollapsedSections();
    if (set.has(sectionId))
        set.delete(sectionId);
    else
        set.add(sectionId);
    persistCollapsedSections();
}
// ── Subsection expand management ────────────────────────────────────────────
function getExpandedSubsections() {
    if (!_expandedSubsections) {
        try {
            const stored = JSON.parse(sessionStorage.getItem('cortex-doc-expanded-subs') || '[]');
            _expandedSubsections = new Set(stored);
        }
        catch {
            _expandedSubsections = new Set();
        }
    }
    return _expandedSubsections;
}
function persistExpandedSubsections() {
    sessionStorage.setItem('cortex-doc-expanded-subs', JSON.stringify([...getExpandedSubsections()]));
}
function toggleSubsectionExpand(sectionId) {
    const set = getExpandedSubsections();
    if (set.has(sectionId))
        set.delete(sectionId);
    else
        set.add(sectionId);
    persistExpandedSubsections();
}
// ── Slug helpers ────────────────────────────────────────────────────────────
export function localSlug(fullId) {
    if (!fullId)
        return '';
    const afterNs = fullId.includes('::') ? fullId.split('::').pop() : fullId;
    const parts = afterNs.split('.');
    return parts[parts.length - 1];
}
function replaceLocalSlug(fullId, newLocal) {
    if (!fullId || !fullId.includes('::'))
        return newLocal;
    const nsIdx = fullId.lastIndexOf('::');
    const prefix = fullId.substring(0, nsIdx + 2);
    const dotPath = fullId.substring(nsIdx + 2);
    const parts = dotPath.split('.');
    parts[parts.length - 1] = newLocal;
    return prefix + parts.join('.');
}
function slugify(text, maxWords = 5, maxLen = 40) {
    let slug = text
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, '-')
        .replace(/^-+|-+$/g, '');
    const words = slug.split('-').filter(Boolean);
    if (words.length > maxWords)
        slug = words.slice(0, maxWords).join('-');
    if (slug.length > maxLen)
        slug = slug.substring(0, maxLen).replace(/-+$/, '');
    return slug || 'section';
}
// ── Section helpers ─────────────────────────────────────────────────────────
function flattenRawSections(sections) {
    const result = [];
    for (const sec of sections) {
        const { sections: children, ...rest } = sec;
        result.push(rest);
        if (children && children.length)
            result.push(...flattenRawSections(children));
    }
    return result;
}
/**
 * Re-nest flat raw sections into a tree using rendered-data depths.
 * Called before saving so the PUT sends properly nested DocJSON.
 */
function nestRawSections(flatSections) {
    const rendered = _renderedData?.sections || [];
    const root = [];
    const stack = [{ target: root, depth: -1 }];
    for (let i = 0; i < flatSections.length; i++) {
        const sec = { ...flatSections[i] };
        const depth = (rendered[i]?.depth) || 0;
        while (stack.length > 1 && stack[stack.length - 1].depth >= depth) {
            stack.pop();
        }
        stack[stack.length - 1].target.push(sec);
        sec.sections = [];
        stack.push({ target: sec.sections, depth });
    }
    const clean = (secs) => {
        for (const s of secs) {
            if (s.sections.length === 0)
                delete s.sections;
            else
                clean(s.sections);
        }
    };
    clean(root);
    return root;
}
function isAncestorCollapsed(sections, idx) {
    const collapsed = getCollapsedSections();
    let sec = sections[idx];
    while (sec && sec.parent_id) {
        if (collapsed.has(sec.parent_id))
            return true;
        sec = sections.find(s => s.id === sec.parent_id);
    }
    return false;
}
function sectionHasChildren(sections, idx) {
    const sec = sections[idx];
    if (!sec)
        return false;
    const depth = sec.depth || 0;
    return idx + 1 < sections.length && (sections[idx + 1].depth || 0) > depth;
}
// ── Nested list builder ─────────────────────────────────────────────────────
function _buildNestedList(block, ordered) {
    const lines = block.trim().split('\n');
    const tag = ordered ? 'ol' : 'ul';
    const pattern = ordered ? /^([ \t]*)\d+\. (.+)$/ : /^([ \t]*)- (.+)$/;
    const indentStack = [];
    let html = '';
    for (const line of lines) {
        const match = line.match(pattern);
        if (!match)
            continue;
        const indent = match[1].length;
        const content = match[2];
        if (indentStack.length === 0) {
            html += `<${tag} class="doc-list">`;
            indentStack.push(indent);
        }
        else if (indent > indentStack[indentStack.length - 1]) {
            html += `<${tag} class="doc-list">`;
            indentStack.push(indent);
        }
        else {
            while (indentStack.length > 1 && indent < indentStack[indentStack.length - 1]) {
                html += `</li></${tag}>`;
                indentStack.pop();
            }
            html += '</li>';
        }
        html += `<li>${content}`;
    }
    while (indentStack.length > 0) {
        html += `</li></${tag}>`;
        indentStack.pop();
    }
    return html;
}
// ── Markdown renderer ───────────────────────────────────────────────────────
export function renderMarkdown(text) {
    if (!text)
        return '<p class="doc-empty-content">No content</p>';
    const sources = [];
    let html = text;
    // Step 1: Extract fenced code blocks into placeholders.
    // They handle their own escaping via esc(); we must protect them
    // from the HTML-escape pass below.
    const codeBlocks = [];
    html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
        let replacement;
        if (lang === 'mermaid') {
            const mIdx = sources.length;
            sources.push(code.trim());
            replacement = `<div class="doc-mermaid-block"><pre class="mermaid" data-mermaid-idx="${mIdx}"></pre></div>`;
        }
        else {
            replacement = `<pre class="doc-code-block"><code>${esc(code)}</code></pre>`;
        }
        const ph = `\x00CB${codeBlocks.length}\x00`;
        codeBlocks.push(replacement);
        return ph;
    });
    // Step 2: HTML-escape the remaining text to prevent XSS.
    // Raw <script>, <iframe>, etc. are neutralized here.
    html = html
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
    // Step 3: Restore fenced code block placeholders.
    codeBlocks.forEach((block, i) => {
        html = html.replace(`\x00CB${i}\x00`, block);
    });
    // Inline code (content is already HTML-escaped from step 2)
    html = html.replace(/`([^`\n]+)`/g, '<code class="doc-inline-code">$1</code>');
    // Tables (simple markdown tables)
    html = html.replace(/((?:^\|.+\|$\n?)+)/gm, (tableBlock) => {
        const rows = tableBlock.trim().split('\n');
        if (rows.length < 2)
            return tableBlock;
        let tableHtml = '<table class="doc-table">';
        rows.forEach((row, i) => {
            // Skip separator row (|---|---|)
            if (/^\|[\s\-:|]+\|$/.test(row))
                return;
            const cells = row.split('|').filter((_, ci) => ci > 0 && ci < row.split('|').length - 1);
            const tag = i === 0 ? 'th' : 'td';
            if (i === 0)
                tableHtml += '<thead>';
            if (i === 2 || (i === 1 && !/^\|[\s\-:|]+\|$/.test(rows[1])))
                tableHtml += '<tbody>';
            tableHtml += '<tr>';
            cells.forEach(cell => {
                tableHtml += `<${tag}>${cell.trim()}</${tag}>`;
            });
            tableHtml += '</tr>';
            if (i === 0)
                tableHtml += '</thead>';
        });
        tableHtml += '</tbody></table>';
        return tableHtml;
    });
    // Headings (### before **, since heading lines may contain bold)
    html = html.replace(/^#{6}\s+(.+)$/gm, '<h6>$1</h6>');
    html = html.replace(/^#{5}\s+(.+)$/gm, '<h5>$1</h5>');
    html = html.replace(/^#{4}\s+(.+)$/gm, '<h4>$1</h4>');
    html = html.replace(/^#{3}\s+(.+)$/gm, '<h3>$1</h3>');
    html = html.replace(/^#{2}\s+(.+)$/gm, '<h2>$1</h2>');
    html = html.replace(/^#{1}\s+(.+)$/gm, '<h1>$1</h1>');
    // Bold
    html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    // Italic
    html = html.replace(/\*([^*]+)\*/g, '<em>$1</em>');
    // Unordered lists (with nesting support)
    html = html.replace(/((?:^[ \t]*- .+$\n?)+)/gm, (block) => {
        return _buildNestedList(block, false);
    });
    // Numbered lists (with nesting support)
    html = html.replace(/((?:^[ \t]*\d+\. .+$\n?)+)/gm, (block) => {
        return _buildNestedList(block, true);
    });
    // Paragraphs (double newlines)
    html = html.replace(/\n\n/g, '</p><p>');
    // Single newlines within a paragraph
    html = html.replace(/\n/g, '<br>');
    // Clean up block elements that got wrapped in <p> tags
    html = html.replace(/<p>\s*(<(?:h[1-6]|table|ul|ol|pre|blockquote|div)\b)/gi, '$1');
    html = html.replace(/(<\/(?:h[1-6]|table|ul|ol|pre|blockquote|div)>)\s*<\/p>/gi, '$1');
    // Remove leftover empty paragraphs
    html = html.replace(/<p>\s*<\/p>/g, '');
    // Remove leading <br> right after a block close or before a block open
    html = html.replace(/(<\/(?:h[1-6]|table|ul|ol|pre|blockquote|div)>)<br>/gi, '$1');
    html = html.replace(/<br>(<(?:h[1-6]|table|ul|ol|pre|blockquote|div)\b)/gi, '$1');
    // Wrap in paragraph if not already wrapped
    if (!html.startsWith('<')) {
        html = `<p>${html}</p>`;
    }
    setMermaidSources(sources);
    return html;
}
// ── HTML detection (for Tiptap content) ─────────────────────────────────────
export function isHtml(content) {
    if (!content)
        return false;
    return content.trim().startsWith('<');
}
export function contentForEditor(content) {
    if (!content)
        return '<p></p>';
    if (isHtml(content))
        return content;
    // One-time conversion: markdown -> HTML via renderMarkdown
    return renderMarkdown(content);
}
// ── Tiptap editor lifecycle ─────────────────────────────────────────────────
export function mountTiptapEditors() {
    if (!window._tiptapReady) {
        console.warn('[docs] Tiptap not ready yet');
        return;
    }
    document.querySelectorAll('.doc-tiptap-editor[data-section-idx]').forEach(el => {
        const idx = el.dataset.sectionIdx || '';
        if (_tiptapEditors[idx])
            return;
        const extensions = [window.TiptapStarterKit];
        if (window.TiptapTable)
            extensions.push(window.TiptapTable.configure({ resizable: false }));
        if (window.TiptapTableRow)
            extensions.push(window.TiptapTableRow);
        if (window.TiptapTableCell)
            extensions.push(window.TiptapTableCell);
        if (window.TiptapTableHeader)
            extensions.push(window.TiptapTableHeader);
        // Read content from sections data (not from data-content attribute which
        // double-escapes HTML entities). Mirrors old _mountTiptapEditors behavior.
        const numIdx = parseInt(idx, 10);
        const sec = _renderedData?.sections?.[numIdx];
        const htmlContent = contentForEditor(sec ? sec.content || '' : '');
        const editor = new window.TiptapEditor({
            element: el,
            extensions,
            content: htmlContent,
            editorProps: {
                attributes: { class: 'doc-tiptap-prosemirror' },
            },
            onUpdate: () => { setDirty(true); },
        });
        // Active toolbar state tracking: toggle .is-active on transaction
        const toolbar = el.previousElementSibling;
        if (toolbar && toolbar.classList.contains('doc-tiptap-toolbar')) {
            editor.on('transaction', () => {
                toolbar.querySelectorAll('.tt-btn').forEach((tbBtn) => {
                    const cmd = tbBtn.dataset.cmd;
                    let isActive = false;
                    switch (cmd) {
                        case 'bold':
                            isActive = editor.isActive('bold');
                            break;
                        case 'italic':
                            isActive = editor.isActive('italic');
                            break;
                        case 'strike':
                            isActive = editor.isActive('strike');
                            break;
                        case 'code':
                            isActive = editor.isActive('code');
                            break;
                        case 'heading':
                            isActive = editor.isActive('heading', { level: parseInt(tbBtn.dataset.level || '3') });
                            break;
                        case 'bulletList':
                            isActive = editor.isActive('bulletList');
                            break;
                        case 'orderedList':
                            isActive = editor.isActive('orderedList');
                            break;
                        case 'codeBlock':
                            isActive = editor.isActive('codeBlock');
                            break;
                        case 'blockquote':
                            isActive = editor.isActive('blockquote');
                            break;
                    }
                    tbBtn.classList.toggle('is-active', isActive);
                });
            });
        }
        _tiptapEditors[idx] = editor;
    });
    // Wire toolbar buttons
    document.querySelectorAll('.doc-tiptap-toolbar').forEach(toolbar => {
        toolbar.querySelectorAll('button[data-cmd]').forEach(btn => {
            btn.addEventListener('click', () => {
                const cmd = btn.dataset.cmd;
                const idx = toolbar.dataset.sectionIdx || '';
                const editor = _tiptapEditors[idx];
                if (!editor)
                    return;
                const chain = editor.chain().focus();
                switch (cmd) {
                    case 'bold':
                        chain.toggleBold().run();
                        break;
                    case 'italic':
                        chain.toggleItalic().run();
                        break;
                    case 'strike':
                        chain.toggleStrike().run();
                        break;
                    case 'code':
                        chain.toggleCode().run();
                        break;
                    case 'heading':
                        chain.toggleHeading({ level: parseInt(btn.dataset.level || '3') }).run();
                        break;
                    case 'bulletList':
                        chain.toggleBulletList().run();
                        break;
                    case 'orderedList':
                        chain.toggleOrderedList().run();
                        break;
                    case 'codeBlock':
                        chain.toggleCodeBlock().run();
                        break;
                    case 'blockquote':
                        chain.toggleBlockquote().run();
                        break;
                    case 'insertTable':
                        chain.insertTable({ rows: 3, cols: 3, withHeaderRow: true }).run();
                        break;
                    case 'addColumnAfter':
                        chain.addColumnAfter().run();
                        break;
                    case 'addRowAfter':
                        chain.addRowAfter().run();
                        break;
                    case 'deleteTable':
                        chain.deleteTable().run();
                        break;
                    case 'hr':
                        chain.setHorizontalRule().run();
                        break;
                    case 'undo':
                        chain.undo().run();
                        break;
                    case 'redo':
                        chain.redo().run();
                        break;
                }
            });
        });
    });
}
export function flushEditors() {
    const rawDoc = getRawDoc();
    for (const [idx, editor] of Object.entries(_tiptapEditors)) {
        const i = parseInt(idx);
        const html = editor.getHTML();
        if (_renderedData && _renderedData.sections && _renderedData.sections[i]) {
            _renderedData.sections[i].content = html;
        }
        if (rawDoc && rawDoc.sections && rawDoc.sections[i]) {
            rawDoc.sections[i].content = html;
        }
    }
}
export function destroyEditors(flush = true) {
    if (flush)
        flushEditors();
    for (const [, editor] of Object.entries(_tiptapEditors)) {
        try {
            editor.destroy();
        }
        catch { }
    }
    _tiptapEditors = {};
}
function destroyScrollObserver() {
    if (_scrollObserver) {
        _scrollObserver.disconnect();
        _scrollObserver = null;
    }
}
// ── Wide mode ───────────────────────────────────────────────────────────────
export function isWide() {
    return localStorage.getItem('cortex-doc-wide') === '1';
}
export function setWide(on) {
    localStorage.setItem('cortex-doc-wide', on ? '1' : '0');
}
// ── Section CRUD ────────────────────────────────────────────────────────────
export function addSection() {
    const rawDoc = getRawDoc();
    if (!rawDoc)
        return;
    if (!rawDoc.sections)
        rawDoc.sections = [];
    const newSlug = `section-${rawDoc.sections.length + 1}`;
    const docId = getSelectedDocId() || '';
    const nsParts = docId.includes('::') ? docId.split('::')[0] + '::' + docId.split('::')[1] : docId;
    const newSection = {
        heading: 'New Section',
        content: '',
        slug: newSlug,
        slug_auto: true,
        depth: 0,
        id: nsParts + '::' + newSlug,
    };
    rawDoc.sections.push(newSection);
    if (_renderedData && _renderedData.sections) {
        _renderedData.sections.push({
            ...newSection,
            linked_nodes: [],
            tags: [],
        });
    }
    setDirty(true);
    _editingHeadingIdx = rawDoc.sections.length - 1;
}
export function addSubSection(parentIdx) {
    const rawDoc = getRawDoc();
    if (!rawDoc || !rawDoc.sections)
        return;
    const parentSec = rawDoc.sections[parentIdx];
    if (!parentSec)
        return;
    const parentDepth = parentSec.depth || 0;
    const newDepth = parentDepth + 1;
    const parentId = parentSec.id || '';
    const newSlug = `sub-${rawDoc.sections.length + 1}`;
    const newId = parentId ? parentId + '.' + newSlug : newSlug;
    const newSection = {
        heading: 'New Sub-section',
        content: '',
        slug: newSlug,
        slug_auto: true,
        depth: newDepth,
        id: newId,
        parent_id: parentId,
    };
    // Insert after parent and all its descendants
    let insertIdx = parentIdx + 1;
    while (insertIdx < rawDoc.sections.length && (rawDoc.sections[insertIdx].depth || 0) > parentDepth) {
        insertIdx++;
    }
    rawDoc.sections.splice(insertIdx, 0, newSection);
    if (_renderedData && _renderedData.sections) {
        _renderedData.sections.splice(insertIdx, 0, {
            ...newSection,
            linked_nodes: [],
            tags: [],
        });
    }
    setDirty(true);
    _editingHeadingIdx = insertIdx;
}
export function moveSection(idx, dir) {
    if (!_renderedData || !_renderedData.sections)
        return;
    const targetIdx = idx + dir;
    if (targetIdx < 0 || targetIdx >= _renderedData.sections.length)
        return;
    const sections = _renderedData.sections;
    [sections[idx], sections[targetIdx]] = [sections[targetIdx], sections[idx]];
    const rawDoc = getRawDoc();
    if (rawDoc && rawDoc.sections) {
        [rawDoc.sections[idx], rawDoc.sections[targetIdx]] = [rawDoc.sections[targetIdx], rawDoc.sections[idx]];
    }
    setDirty(true);
    // Update tracked indices
    if (_editingSectionIdx === idx)
        _editingSectionIdx = targetIdx;
    else if (_editingSectionIdx === targetIdx)
        _editingSectionIdx = idx;
    if (_editingHeadingIdx === idx)
        _editingHeadingIdx = targetIdx;
    else if (_editingHeadingIdx === targetIdx)
        _editingHeadingIdx = idx;
    if (_editingSlugIdx === idx)
        _editingSlugIdx = targetIdx;
    else if (_editingSlugIdx === targetIdx)
        _editingSlugIdx = idx;
    if (_linkSearchIdx === idx)
        _linkSearchIdx = targetIdx;
    else if (_linkSearchIdx === targetIdx)
        _linkSearchIdx = idx;
}
export function removeSection(idx) {
    const rawDoc = getRawDoc();
    if (rawDoc && rawDoc.sections) {
        rawDoc.sections.splice(idx, 1);
    }
    if (_renderedData && _renderedData.sections) {
        _renderedData.sections.splice(idx, 1);
    }
    setDirty(true);
    if (_editingSectionIdx === idx)
        _editingSectionIdx = null;
    if (_editingHeadingIdx === idx)
        _editingHeadingIdx = null;
    if (_editingSlugIdx === idx)
        _editingSlugIdx = null;
    if (_linkSearchIdx === idx)
        _linkSearchIdx = null;
}
// ── Tag management ──────────────────────────────────────────────────────────
export function addDocTag(tag) {
    const rawDoc = getRawDoc();
    if (!rawDoc)
        return;
    if (!rawDoc.tags)
        rawDoc.tags = [];
    if (rawDoc.tags.includes(tag))
        return;
    rawDoc.tags.push(tag);
    setDirty(true);
}
export function removeDocTag(tagIdx) {
    const rawDoc = getRawDoc();
    if (!rawDoc || !rawDoc.tags)
        return;
    rawDoc.tags.splice(tagIdx, 1);
    setDirty(true);
}
export function addSectionTag(secIdx, tag) {
    const rawDoc = getRawDoc();
    if (!rawDoc || !rawDoc.sections || !rawDoc.sections[secIdx])
        return;
    const sec = rawDoc.sections[secIdx];
    if (!sec.tags)
        sec.tags = [];
    if (sec.tags.includes(tag))
        return;
    sec.tags.push(tag);
    if (_renderedData && _renderedData.sections && _renderedData.sections[secIdx]) {
        if (!_renderedData.sections[secIdx].tags)
            _renderedData.sections[secIdx].tags = [];
        _renderedData.sections[secIdx].tags.push(tag);
    }
    setDirty(true);
}
export function removeSectionTag(secIdx, tagIdx) {
    const rawDoc = getRawDoc();
    if (!rawDoc || !rawDoc.sections || !rawDoc.sections[secIdx])
        return;
    const sec = rawDoc.sections[secIdx];
    if (!sec.tags)
        return;
    sec.tags.splice(tagIdx, 1);
    if (_renderedData && _renderedData.sections && _renderedData.sections[secIdx] && _renderedData.sections[secIdx].tags) {
        _renderedData.sections[secIdx].tags.splice(tagIdx, 1);
    }
    setDirty(true);
}
// ── Link management ─────────────────────────────────────────────────────────
export async function doLinkSearch(query, sectionIdx) {
    if (!query) {
        _linkSearchResults = [];
        return;
    }
    try {
        const data = await apiFetch(`/api/search?q=${encodeURIComponent(query)}`);
        _linkSearchResults = data.nodes || [];
    }
    catch {
        _linkSearchResults = [];
    }
}
export function getLinkSearchResults() { return _linkSearchResults; }
export function addLink(sectionIdx, nodeId) {
    const rawDoc = getRawDoc();
    if (!rawDoc || !rawDoc.sections || !rawDoc.sections[sectionIdx])
        return;
    const sec = rawDoc.sections[sectionIdx];
    if (!sec.links)
        sec.links = [];
    if (sec.links.some((l) => l.node_id === nodeId))
        return;
    sec.links.push({ node_id: nodeId, relationship: 'documents' });
    if (_renderedData && _renderedData.sections && _renderedData.sections[sectionIdx]) {
        const renderedSec = _renderedData.sections[sectionIdx];
        if (!renderedSec.linked_nodes)
            renderedSec.linked_nodes = [];
        const match = _linkSearchResults.find(n => n.id === nodeId);
        renderedSec.linked_nodes.push({
            node_id: nodeId,
            title: match ? match.title : nodeId.split('::').pop(),
            node_type: match ? match.node_type : 'unknown',
            relationship: 'documents',
        });
    }
    setDirty(true);
}
export function removeLink(sectionIdx, linkIdx) {
    const rawDoc = getRawDoc();
    if (!rawDoc || !rawDoc.sections || !rawDoc.sections[sectionIdx])
        return;
    const sec = rawDoc.sections[sectionIdx];
    if (!sec.links)
        return;
    const removedNodeId = sec.links[linkIdx]?.node_id;
    sec.links.splice(linkIdx, 1);
    if (_renderedData && _renderedData.sections && _renderedData.sections[sectionIdx]) {
        const renderedSec = _renderedData.sections[sectionIdx];
        if (renderedSec.linked_nodes && removedNodeId) {
            const rIdx = renderedSec.linked_nodes.findIndex(n => n.node_id === removedNodeId);
            if (rIdx >= 0)
                renderedSec.linked_nodes.splice(rIdx, 1);
        }
    }
    setDirty(true);
}
// ── Save ────────────────────────────────────────────────────────────────────
export async function save() {
    const rawDoc = getRawDoc();
    const docId = getSelectedDocId();
    if (!rawDoc || !docId)
        return false;
    flushEditors();
    // Validate sections
    const sections = _renderedData?.sections || [];
    const errors = [];
    // Check empty headings
    sections.forEach((sec, idx) => {
        if (!sec.heading || !sec.heading.trim()) {
            errors.push({ idx, msg: 'Empty heading \u2014 please enter a section name' });
        }
    });
    // Check duplicate slugs (within same parent scope)
    const slugsByParent = {};
    sections.forEach((sec, idx) => {
        const key = sec.parent_id || '__root__';
        if (!slugsByParent[key])
            slugsByParent[key] = [];
        const existing = slugsByParent[key].find(s => s.id === sec.id);
        if (existing) {
            errors.push({ idx, msg: `Slug "${sec.id}" already used by another section` });
        }
        slugsByParent[key].push({ id: sec.id, idx });
    });
    if (errors.length > 0) {
        // Show inline validation errors on each section
        errors.forEach(e => {
            const secEl = document.querySelector(`.doc-section[data-idx="${e.idx}"]`);
            if (secEl) {
                secEl.classList.add('doc-section--error');
                let errDiv = secEl.querySelector('.doc-section-error');
                if (!errDiv) {
                    errDiv = document.createElement('div');
                    errDiv.className = 'doc-section-error';
                    const header = secEl.querySelector('.doc-section-header');
                    if (header)
                        header.after(errDiv);
                }
                errDiv.textContent = '\u26A0 ' + e.msg;
            }
        });
        const saveBtn = document.getElementById('doc-save-btn');
        if (saveBtn)
            saveBtn.textContent = `Save blocked \u2014 ${errors.length} issue${errors.length > 1 ? 's' : ''}`;
        setTimeout(() => { const btn = document.getElementById('doc-save-btn'); if (btn)
            btn.textContent = 'Save'; }, 3000);
        return false;
    }
    const saveBtn = document.getElementById('doc-save-btn');
    if (saveBtn) {
        saveBtn.textContent = 'Saving\u2026';
        saveBtn.classList.add('disabled');
    }
    try {
        // Prepare doc to save -- nest sections back into tree before sending PUT
        const docToSave = { ...rawDoc };
        if (docToSave.sections && docToSave.sections.length > 0) {
            docToSave.sections = nestRawSections(docToSave.sections);
        }
        await saveDoc(docId, docToSave, getPendingRenames());
        clearPendingRenames();
        // Re-fetch rendered data to pick up any changes in linked node summaries
        const [renderData, rawData] = await Promise.all([
            apiFetch(`/api/docs/${encodeURIComponent(docId)}/render`),
            apiFetch(`/api/docs/${encodeURIComponent(docId)}/raw`),
        ]);
        _renderedData = renderData;
        const fetchedDoc = rawData.doc;
        if (fetchedDoc && fetchedDoc.sections) {
            setRawDoc({ ...fetchedDoc, sections: flattenRawSections(fetchedDoc.sections) });
        }
        else {
            setRawDoc(fetchedDoc);
        }
        _rerender();
        if (saveBtn)
            saveBtn.textContent = '\u2713 Saved';
        setTimeout(() => { const btn = document.getElementById('doc-save-btn'); if (btn)
            btn.textContent = 'Save'; }, 2000);
        return true;
    }
    catch (err) {
        if (saveBtn) {
            saveBtn.textContent = 'Save failed';
            saveBtn.classList.remove('disabled');
        }
        console.error('Doc save error:', err);
        return false;
    }
}
// ── TOC rendering ───────────────────────────────────────────────────────────
export function renderToc(sections) {
    if (!sections || sections.length === 0)
        return '';
    let html = '<div class="doc-toc">';
    html += '<div class="doc-toc-header">Contents</div>';
    for (let i = 0; i < sections.length; i++) {
        const sec = sections[i];
        const depth = sec.depth || 0;
        const indent = depth * 12 + 14;
        html += `<a class="doc-toc-item" data-section-idx="${i}" style="padding-left:${indent}px">${esc(sec.heading)}</a>`;
    }
    html += '</div>';
    return html;
}
// Re-export runMermaid from doc-diagrams for convenience
export async function runMermaid() {
    return runMermaidDiagrams();
}
// ── Raw source toggle ────────────────────────────────────────────────────────
/**
 * Toggle between raw JSON source and rendered view.
 * When switching FROM raw source TO rendered, validates JSON, applies changes,
 * saves immediately, and re-fetches rendered data. Shows inline errors if
 * the JSON is invalid.
 */
async function toggleRawSource() {
    if (_viewRaw) {
        // Switching FROM raw source TO rendered
        const errorEl = document.getElementById('doc-raw-source-error');
        // Get current text from Monaco or textarea fallback
        let currentText;
        const rawMonaco = getRawMonaco();
        if (rawMonaco) {
            currentText = rawMonaco.getValue();
        }
        else {
            const textarea = document.getElementById('doc-raw-source-textarea');
            currentText = textarea ? textarea.value : '';
        }
        // Check if content was actually changed
        const originalText = JSON.stringify(getRawDoc() || {}, null, 2);
        const changed = currentText.trim() !== originalText.trim();
        if (changed) {
            // Parse & validate the edited JSON
            let parsed;
            try {
                parsed = JSON.parse(currentText);
            }
            catch (e) {
                if (errorEl) {
                    errorEl.textContent = 'Invalid JSON: ' + e.message;
                    errorEl.style.display = 'block';
                }
                return; // stay in raw view
            }
            if (typeof parsed !== 'object' || Array.isArray(parsed)) {
                if (errorEl) {
                    errorEl.textContent = 'JSON must be an object (not an array)';
                    errorEl.style.display = 'block';
                }
                return;
            }
            if (!parsed.title || !String(parsed.title).trim()) {
                if (errorEl) {
                    errorEl.textContent = 'JSON must have a non-empty "title" field';
                    errorEl.style.display = 'block';
                }
                return;
            }
            // Apply changes
            setRawDoc(parsed);
            setDirty(true);
            // Save immediately then re-fetch rendered
            const docId = getSelectedDocId();
            try {
                await save();
                if (docId) {
                    const [renderData, rawData] = await Promise.all([
                        apiFetch(`/api/docs/${encodeURIComponent(docId)}/render`),
                        apiFetch(`/api/docs/${encodeURIComponent(docId)}/raw`),
                    ]);
                    _renderedData = renderData;
                    setRawDoc(rawData.doc);
                    if (rawData.doc && rawData.doc.sections) {
                        setRawDoc({ ...rawData.doc, sections: flattenRawSections(rawData.doc.sections) });
                    }
                }
            }
            catch (err) {
                if (errorEl) {
                    errorEl.textContent = 'Save failed: ' + err.message;
                    errorEl.style.display = 'block';
                }
                return;
            }
        }
        // Destroy Monaco before switching view
        destroyRawMonaco();
        _viewRaw = false;
        _rerender();
    }
    else {
        // Switching TO raw source -- destroy any active editors first
        destroyEditors(false);
        destroyDiagramEditor();
        _editingSectionIdx = null;
        _viewRaw = true;
        _rerender();
    }
}
// ── Full interactive doc content rendering ───────────────────────────────────
/** Callback type for re-rendering. Set by app.ts via setRerenderCallback. */
let _rerenderFn = null;
export function setRerenderCallback(fn) {
    _rerenderFn = fn;
}
function _rerender() {
    if (_rerenderFn)
        _rerenderFn();
}
/**
 * Render the full interactive doc content into the given container element.
 * Includes section headers, edit buttons, Tiptap editors, link bars, tag bars,
 * raw JSON toggle, wide mode, save button, and diagram editing.
 */
export function renderDocContent(contentEl) {
    if (!_renderedData)
        return;
    // Reset mermaid source collector
    clearMermaidSources();
    const { title, sections } = _renderedData;
    const rawDoc = getRawDoc() || {};
    const docTags = rawDoc.tags || [];
    const diagramEditorIdx = getDiagramEditorIdx();
    let html = `
    <div class="doc-content" id="doc-content">
    <div class="doc-content-inner${isWide() ? ' wide' : ''}">
      <div class="doc-header">
        <h1 class="doc-title">${esc(title)}</h1>
        <div class="doc-actions">
          <button class="doc-source-toggle${_viewRaw ? ' active' : ''}"
                  id="doc-source-toggle" title="${_viewRaw ? 'Switch to rendered view' : 'View raw JSON source'}">
            ${_viewRaw ? '&#x2726; Rendered' : '{ } Source'}
          </button>
          <button class="doc-wide-toggle${isWide() ? ' active' : ''}"
                  id="doc-wide-toggle" title="Toggle full-width view">
            ${isWide() ? '&#x21E4; Narrow' : '&#x21E5; Wide'}
          </button>
          <button class="doc-save-btn${isDirty() ? '' : ' disabled'}"
                  id="doc-save-btn" title="Save changes back to JSON file">
            Save
          </button>
        </div>
      </div>

      ${_viewRaw ? '' : `
      <div class="doc-tags-bar" id="doc-tags-bar">
        <span class="doc-tags-label">Tags:</span>
        <div class="doc-tags-chips" id="doc-tags-chips">
          ${docTags.map((t, ti) => `
            <span class="doc-tag-chip">
              ${esc(t)}
              <button class="doc-tag-remove" data-scope="doc" data-tag-idx="${ti}" title="Remove tag">&#x2715;</button>
            </span>
          `).join('')}
          <span class="doc-tag-add-wrap">
            <input type="text" class="doc-tag-add-input" id="doc-tag-add-input"
                   placeholder="+ add tag" maxlength="40" spellcheck="false">
          </span>
        </div>
      </div>`}

      <div class="doc-sections" id="doc-sections">`;
    // ── Raw source view ──
    if (_viewRaw) {
        const jsonStr = JSON.stringify(rawDoc, null, 2);
        html += `
        <div class="doc-raw-source-wrap">
          <div class="doc-raw-source-error" id="doc-raw-source-error"></div>
          <div class="doc-raw-source-monaco" id="doc-raw-source-monaco"></div>
          <textarea class="doc-raw-source-textarea" id="doc-raw-source-textarea"
            spellcheck="false">${esc(jsonStr)}</textarea>
        </div>
      </div>
    </div>
    </div>`;
        contentEl.innerHTML = html;
        _wireRawSourceEvents(contentEl);
        _mountRawMonaco();
        return;
    }
    // ── Sort sections into depth-first (parent → children) order ──
    // The render API may return sections ordered by position within each parent,
    // which interleaves children from different parents. Re-order to tree walk.
    if (sections && sections.length > 1) {
        const byId = new Map();
        const childMap = new Map();
        for (const sec of sections) {
            byId.set(sec.id, sec);
            const pid = sec.parent_id || '';
            if (!childMap.has(pid))
                childMap.set(pid, []);
            childMap.get(pid).push(sec);
        }
        const ordered = [];
        const walk = (parentId) => {
            const children = childMap.get(parentId) || [];
            for (const child of children) {
                ordered.push(child);
                walk(child.id);
            }
        };
        walk('');
        if (ordered.length === sections.length) {
            // Re-index: update _renderedData.sections in-place so idx values stay consistent
            for (let i = 0; i < ordered.length; i++) {
                sections[i] = ordered[i];
            }
        }
    }
    // ── Section rendering ──
    if (sections) {
        let inSubContainer = false;
        let parentOpen = false;
        let deferredParentChrome = '';
        sections.forEach((sec, idx) => {
            const isEditing = _editingSectionIdx === idx;
            const isDiagramEditing = diagramEditorIdx === idx;
            const isEditingHeading = _editingHeadingIdx === idx;
            const isLinkSearch = _linkSearchIdx === idx;
            const hasMermaid = isMermaidSection(sec.content);
            const rawSec = rawDoc.sections ? rawDoc.sections[idx] : null;
            const secLinks = rawSec ? (rawSec.links || []) : [];
            const secTags = rawSec ? (rawSec.tags || []) : [];
            const isFirst = idx === 0;
            const isLast = idx === sections.length - 1;
            const isDiagramReadMode = hasMermaid && !isEditing && !isDiagramEditing;
            // Nesting support
            const secDepth = sec.depth || 0;
            const hasChildren = sectionHasChildren(sections, idx);
            const isCollapsed = hasChildren && getCollapsedSections().has(sec.id);
            const isHiddenByParent = isAncestorCollapsed(sections, idx);
            // ── Shared rendering helpers ──
            const contentHtml = () => {
                if (isDiagramEditing) {
                    return `
          <div class="doc-diagram-editor-wrap" data-idx="${idx}">
            <div class="doc-diagram-split">
              <div class="doc-diagram-code-pane">
                <div class="doc-diagram-code-header">Mermaid Source</div>
                <div id="diagram-editor-${idx}" class="doc-diagram-monaco"></div>
              </div>
              <div class="doc-diagram-preview-pane">
                <div class="doc-diagram-preview-header">Preview</div>
                <div id="diagram-preview-${idx}" class="doc-diagram-preview"></div>
              </div>
            </div>
            <div class="doc-diagram-actions">
              <button class="doc-diagram-apply-btn" data-idx="${idx}">Apply</button>
              <button class="doc-diagram-cancel-btn" data-idx="${idx}">Cancel</button>
            </div>
          </div>`;
                }
                else if (isEditing) {
                    return `
          <div class="doc-section-edit">
            <div class="doc-tiptap-toolbar" data-section-idx="${idx}">
              <button class="tt-btn" data-cmd="bold" title="Bold (Ctrl+B)"><strong>B</strong></button>
              <button class="tt-btn" data-cmd="italic" title="Italic (Ctrl+I)"><em>I</em></button>
              <button class="tt-btn" data-cmd="strike" title="Strikethrough"><s>S</s></button>
              <button class="tt-btn" data-cmd="code" title="Inline code">&lt;/&gt;</button>
              <span class="tt-sep"></span>
              <button class="tt-btn" data-cmd="heading" data-level="2" title="Heading 2">H2</button>
              <button class="tt-btn" data-cmd="heading" data-level="3" title="Heading 3">H3</button>
              <span class="tt-sep"></span>
              <button class="tt-btn" data-cmd="bulletList" title="Bullet list">&#x2022; List</button>
              <button class="tt-btn" data-cmd="orderedList" title="Numbered list">1. List</button>
              <button class="tt-btn" data-cmd="codeBlock" title="Code block">Code</button>
              <button class="tt-btn" data-cmd="blockquote" title="Blockquote">&ldquo; Quote</button>
              <span class="tt-sep"></span>
              <button class="tt-btn" data-cmd="insertTable" title="Insert table">&#x229E; Table</button>
              <button class="tt-btn" data-cmd="addColumnAfter" title="Add column">Col+</button>
              <button class="tt-btn" data-cmd="addRowAfter" title="Add row">Row+</button>
              <button class="tt-btn" data-cmd="deleteTable" title="Delete table">&#x229F; Table</button>
              <span class="tt-sep"></span>
              <button class="tt-btn" data-cmd="hr" title="Horizontal rule">&#x2015;</button>
              <button class="tt-btn" data-cmd="undo" title="Undo (Ctrl+Z)">&hookleftarrow;</button>
              <button class="tt-btn" data-cmd="redo" title="Redo (Ctrl+Shift+Z)">&hookrightarrow;</button>
            </div>
            <div class="doc-tiptap-editor" data-section-idx="${idx}"></div>
            <div class="doc-section-edit-actions">
              <button class="doc-apply-btn" data-idx="${idx}">Apply</button>
            </div>
          </div>`;
                }
                else {
                    if (isHtml(sec.content)) {
                        return `<div class="doc-section-content">${sec.content}</div>`;
                    }
                    else {
                        return `<div class="doc-section-content">${renderMarkdown(sec.content || '')}</div>`;
                    }
                }
            };
            const tagsHtml = () => `
        <div class="doc-section-tags-bar" data-idx="${idx}">
          <span class="doc-stags-label">Tags:</span>
          ${secTags.map((t, ti) => `
            <span class="doc-section-tag-chip">
              ${esc(t)}
              <button class="doc-tag-remove" data-scope="section" data-section-idx="${idx}" data-tag-idx="${ti}" title="Remove tag">&#x2715;</button>
            </span>
          `).join('')}
          <input type="text" class="doc-stag-add-input" data-idx="${idx}"
                 placeholder="+ tag" maxlength="40" spellcheck="false">
        </div>`;
            const linksHtml = () => {
                const displayLinks = sec.linked_nodes || [];
                let out = `
        <div class="doc-link-bar" data-idx="${idx}">
          <div class="doc-link-label">Links:</div>
          <div class="doc-link-chips">`;
                secLinks.forEach((lnk, li) => {
                    const display = displayLinks.find((d) => d.node_id === lnk.node_id);
                    const label = display
                        ? (display.node_id.split('::').pop())
                        : (lnk.node_id.split('::').pop());
                    out += `
            <span class="doc-link-chip" data-node-id="${esc(lnk.node_id)}" title="${esc(lnk.node_id)}">
              <span class="doc-link-chip-label">${esc(label)}</span>
              <button class="doc-link-remove" data-section-idx="${idx}" data-link-idx="${li}" title="Remove link">&#x2715;</button>
            </span>`;
                });
                out += `
          </div>
          <div class="doc-link-add-wrap">
            <button class="doc-link-add-btn" data-idx="${idx}" title="Search and add a linked node">+ Link</button>`;
                if (isLinkSearch) {
                    out += `
            <div class="doc-link-search-panel" data-idx="${idx}">
              <input type="text" class="doc-link-search-input" data-idx="${idx}"
                     placeholder="Search nodes..." autocomplete="off" spellcheck="false">
              <div class="doc-link-search-results" data-idx="${idx}">
                ${_renderLinkSearchResults(idx, secLinks)}
              </div>
              <button class="doc-link-search-close" data-idx="${idx}">Close</button>
            </div>`;
                }
                out += `
          </div>
        </div>`;
                return out;
            };
            // ── Close sub-container (+ parent section if one is open) ──
            if (secDepth < 1 && inSubContainer) {
                html += `</div>`; // close .sub-container
                if (parentOpen) {
                    html += deferredParentChrome;
                    deferredParentChrome = '';
                    html += `</div>`; // close parent .doc-section
                    parentOpen = false;
                }
                inSubContainer = false;
            }
            if (secDepth >= 1) {
                // ── Subsection: compact accordion row ──
                if (!inSubContainer) {
                    html += `<div class="sub-container">`;
                    inSubContainer = true;
                }
                const isSubExpanded = getExpandedSubsections().has(sec.id) || isEditing || isDiagramEditing;
                let childCount = 0;
                for (let j = idx + 1; j < sections.length && (sections[j].depth || 0) > secDepth; j++) {
                    if ((sections[j].depth || 0) === secDepth + 1)
                        childCount++;
                }
                html += `
          <div class="sub-row${isSubExpanded ? ' expanded' : ''}${isEditing || isDiagramEditing ? ' editing' : ''}" data-idx="${idx}" data-section-id="${esc(sec.id)}">
            <div class="sub-row-header" data-section-id="${esc(sec.id)}">
              <span class="sub-chevron">${isSubExpanded ? '&#x25be;' : '&#x25b8;'}</span>
              <span class="sub-heading">${esc(sec.heading)}</span>
              ${childCount > 0 ? `<span class="sub-count">${childCount}</span>` : ''}
              ${isSubExpanded ? `<div class="sub-header-actions">
                <button class="doc-move-btn" data-idx="${idx}" data-dir="up" title="Move up"${isFirst ? ' disabled' : ''}>&#x25b2;</button>
                <button class="doc-move-btn" data-idx="${idx}" data-dir="down" title="Move down"${isLast ? ' disabled' : ''}>&#x25bc;</button>
                <button class="doc-edit-btn" data-idx="${idx}" title="Edit this section">${isEditing ? '&#x2715;' : '&#x270E;'}</button>
                <button class="doc-remove-section-btn" data-idx="${idx}" title="Remove section">&#x1F5D1;</button>
              </div>` : ''}
            </div>`;
                if (isSubExpanded) {
                    html += contentHtml();
                    // Compact metadata footer
                    const displayLinks = sec.linked_nodes || [];
                    html += `<div class="sub-meta-footer" data-idx="${idx}">`;
                    secTags.forEach((t, ti) => {
                        html += `<span class="sub-tag">${esc(t)}<button class="doc-tag-remove" data-scope="section" data-section-idx="${idx}" data-tag-idx="${ti}" title="Remove tag">&#x2715;</button></span>`;
                    });
                    html += `<span class="sub-tag-add" data-idx="${idx}">+tag</span>`;
                    html += `<span class="sub-meta-sep"></span>`;
                    secLinks.forEach((lnk, li) => {
                        const display = displayLinks.find((d) => d.node_id === lnk.node_id);
                        const label = display ? display.node_id.split('::').pop() : lnk.node_id.split('::').pop();
                        html += `<span class="sub-link" data-node-id="${esc(lnk.node_id)}" title="${esc(lnk.node_id)}"><span class="sub-link-label">${esc(label)}</span><button class="doc-link-remove" data-section-idx="${idx}" data-link-idx="${li}" title="Remove link">&#x2715;</button></span>`;
                    });
                    html += `<span class="sub-link-add" data-idx="${idx}">+link</span>`;
                    if (isLinkSearch) {
                        html += `
              <div class="doc-link-search-panel sub-link-search-panel" data-idx="${idx}">
                <input type="text" class="doc-link-search-input" data-idx="${idx}"
                       placeholder="Search nodes..." autocomplete="off" spellcheck="false">
                <div class="doc-link-search-results" data-idx="${idx}">
                  ${_renderLinkSearchResults(idx, secLinks)}
                </div>
                <button class="doc-link-search-close" data-idx="${idx}">Close</button>
              </div>`;
                    }
                    html += `</div>`;
                }
                html += `</div>`; // close .sub-row
                // Close sub-container (+ parent) if next section exits depth-1
                const nextDepth = (idx + 1 < sections.length) ? (sections[idx + 1].depth || 0) : 0;
                if (nextDepth < 1) {
                    html += `</div>`; // close .sub-container
                    if (parentOpen) {
                        html += deferredParentChrome;
                        deferredParentChrome = '';
                        html += `</div>`; // close parent .doc-section
                        parentOpen = false;
                    }
                    inSubContainer = false;
                }
            }
            else {
                // ── Depth 0: full section card ──
                const emptyParent = !sec.content?.trim() && hasChildren;
                html += `
        <div class="doc-section${isEditing ? ' editing' : ''}${isDiagramReadMode ? ' doc-section--diagram' : ''}${isHiddenByParent ? ' hidden' : ''}" data-idx="${idx}" data-depth="${secDepth}" data-section-id="${esc(sec.id)}">
          <div class="doc-section-header">
            ${hasChildren ? `<button class="doc-collapse-toggle" data-section-id="${esc(sec.id)}" title="${isCollapsed ? 'Expand sub-sections' : 'Collapse sub-sections'}">${isCollapsed ? '&#x25b8;' : '&#x25be;'}</button>` : ''}
            <span class="doc-section-level">${'#'.repeat(Math.max(2, Math.min((sec.level || 2), 6)))}</span>`;
                // Heading + slug wrapper
                html += `<div class="doc-heading-slug-wrap">`;
                if (isEditingHeading) {
                    html += `
            <input type="text" class="doc-heading-input" data-idx="${idx}"
                   value="${esc(sec.heading)}" spellcheck="false">
            <button class="doc-heading-save-btn" data-idx="${idx}" title="Confirm heading">&#x2713;</button>
            <button class="doc-heading-cancel-btn" data-idx="${idx}" title="Cancel">&#x2715;</button>`;
                }
                else {
                    html += `
            <span class="doc-section-heading" data-idx="${idx}" title="Click to edit heading">${esc(sec.heading)}</span>`;
                    if (_editingSlugIdx === idx) {
                        html += `
            <span class="doc-slug-sep">&middot;</span>
            <input type="text" class="doc-slug-input" data-idx="${idx}"
                   value="${esc(localSlug(sec.id))}" spellcheck="false">
            <button class="doc-slug-save-btn" data-idx="${idx}" title="Accept slug">&#x2713;</button>
            <button class="doc-slug-cancel-btn" data-idx="${idx}" title="Cancel">&#x2715;</button>
            <span class="doc-slug-conflict" data-idx="${idx}"></span>`;
                    }
                    else {
                        html += `
            <span class="doc-slug-sep">&middot;</span>
            <span class="doc-slug-text" data-idx="${idx}" title="Click to edit section ID">${esc(localSlug(sec.id))}</span>
            ${sec.slug_auto ? '<span class="doc-slug-auto-badge">AUTO</span>' : ''}`;
                    }
                }
                html += `</div>`;
                // Section action buttons
                html += `
            <div class="doc-section-actions">
              <button class="doc-move-btn" data-idx="${idx}" data-dir="up" title="Move up"${isFirst ? ' disabled' : ''}>&#x25b2;</button>
              <button class="doc-move-btn" data-idx="${idx}" data-dir="down" title="Move down"${isLast ? ' disabled' : ''}>&#x25bc;</button>
              ${isDiagramReadMode ? `<button class="doc-diagram-expand-btn" data-idx="${idx}" title="Toggle full-width diagram">&#x2922; Expand</button>` : ''}
              <button class="doc-diagram-btn${isDiagramEditing ? ' active' : ''}${hasMermaid ? ' has-mermaid' : ''}" data-idx="${idx}" title="${isDiagramEditing ? 'Close diagram editor' : 'Edit as Mermaid diagram'}">
                ${isDiagramEditing ? '&#x2715; Diagram' : '&#x25C8; Diagram'}
              </button>
              <button class="doc-edit-btn" data-idx="${idx}" title="Edit this section">
                ${isEditing ? '&#x2715; Cancel' : '&#x270E; Edit'}
              </button>
              <button class="doc-remove-section-btn" data-idx="${idx}" title="Remove section">&#x1F5D1;</button>
              <button class="doc-add-subsection-btn" data-idx="${idx}" title="Add sub-section">+ Sub</button>
            </div>
          </div>`;
                if (hasChildren) {
                    // Parent: content first, then sub-container (rendered by
                    // subsequent depth-1 iterations), then tags/links deferred
                    // to after sub-container closes.
                    if (!emptyParent) {
                        html += contentHtml();
                    }
                    deferredParentChrome = tagsHtml() + linksHtml();
                    parentOpen = true;
                }
                else {
                    // Leaf: content first, then chrome
                    html += contentHtml();
                    html += tagsHtml();
                    html += linksHtml();
                    html += `</div>`; // close .doc-section
                }
            }
        });
        if (inSubContainer) {
            html += `</div>`; // close .sub-container
            html += deferredParentChrome;
            deferredParentChrome = '';
            html += `</div>`; // close parent .doc-section
        }
    }
    // Add Section button
    html += `
      </div>
      <div class="doc-add-section-wrap">
        <button class="doc-add-section-btn" id="doc-add-section-btn" title="Add a new section">+ Add Section</button>
      </div>
    </div>
    </div>`;
    // TOC sidebar (right)
    html += renderToc(sections || []);
    contentEl.innerHTML = html;
    // Wire events
    wireDocEvents(contentEl);
    // Mount Tiptap editors
    mountTiptapEditors();
    // Mount diagram editor if one is open
    if (diagramEditorIdx !== null) {
        const sec = _renderedData?.sections?.[diagramEditorIdx];
        if (sec) {
            const source = extractMermaidSource(sec.content);
            mountDiagramEditor(diagramEditorIdx, source);
        }
    }
    // Render mermaid diagrams in read mode
    runMermaidDiagrams();
    // Build TOC sidebar
    _renderTocSidebar(contentEl);
}
function _renderLinkSearchResults(sectionIdx, existingLinks) {
    const existingIds = new Set(existingLinks.map((l) => l.node_id));
    const results = getLinkSearchResults();
    if (results.length === 0) {
        return '<div class="doc-link-search-empty">Type to search nodes...</div>';
    }
    return results.map(node => {
        const already = existingIds.has(node.id);
        return `
      <div class="doc-link-search-item${already ? ' already-linked' : ''}"
           data-section-idx="${sectionIdx}" data-node-id="${esc(node.id)}">
        <span class="doc-link-search-id">${esc(node.id.split('::').pop())}</span>
        <span class="doc-link-search-type">${esc(node.node_type || '')}</span>
        ${already ? '<span class="doc-link-search-badge">linked</span>' : ''}
      </div>`;
    }).join('');
}
function _renderTocSidebar(contentEl) {
    if (!_renderedData || !_renderedData.sections)
        return;
    destroyScrollObserver();
    const scrollRoot = contentEl.querySelector('#doc-content');
    const sectionEls = contentEl.querySelectorAll('.doc-section[data-idx]');
    if (sectionEls.length === 0)
        return;
    _scrollObserver = new IntersectionObserver((entries) => {
        for (const entry of entries) {
            if (entry.isIntersecting) {
                const idx = entry.target.dataset.idx;
                document.querySelectorAll('.doc-toc-item').forEach(el => {
                    el.classList.toggle('active', el.dataset.sectionIdx === idx);
                });
            }
        }
    }, {
        root: scrollRoot,
        rootMargin: '-10% 0px -80% 0px',
        threshold: 0,
    });
    sectionEls.forEach(el => _scrollObserver.observe(el));
}
function _wireRawSourceEvents(contentEl) {
    const textarea = contentEl.querySelector('#doc-raw-source-textarea');
    if (textarea) {
        textarea.addEventListener('input', () => { setDirty(true); });
    }
}
function _mountRawMonaco() {
    const monacoEl = document.getElementById('doc-raw-source-monaco');
    if (!monacoEl)
        return;
    const m = window.monaco;
    if (!m)
        return;
    const rawDoc = getRawDoc();
    const jsonStr = JSON.stringify(rawDoc || {}, null, 2);
    const editor = m.editor.create(monacoEl, {
        value: jsonStr,
        language: 'json',
        theme: 'vs-dark',
        minimap: { enabled: false },
        scrollBeyondLastLine: false,
        fontSize: 12,
        automaticLayout: true,
        wordWrap: 'on',
    });
    editor.onDidChangeModelContent(() => { setDirty(true); });
    setRawMonaco(editor);
}
/**
 * Wire all interactive event handlers for the rendered doc content.
 * Called after innerHTML is set by renderDocContent.
 */
export function wireDocEvents(contentEl) {
    // Edit buttons (content)
    contentEl.querySelectorAll('.doc-edit-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const idx = parseInt(btn.dataset.idx || '0', 10);
            destroyDiagramEditor();
            if (_editingSectionIdx === idx) {
                destroyEditors(false);
                _editingSectionIdx = null;
            }
            else {
                destroyEditors(false);
                _editingSectionIdx = idx;
            }
            _rerender();
        });
    });
    // Diagram expand toggle
    contentEl.querySelectorAll('.doc-diagram-expand-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const section = btn.closest('.doc-section--diagram');
            if (!section)
                return;
            const isExpanded = section.classList.toggle('diagram-expanded');
            btn.classList.toggle('active', isExpanded);
            btn.innerHTML = isExpanded ? '&#x2921; Collapse' : '&#x2922; Expand';
        });
    });
    // Diagram editor buttons
    contentEl.querySelectorAll('.doc-diagram-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const idx = parseInt(btn.dataset.idx || '0', 10);
            destroyEditors(false);
            _editingSectionIdx = null;
            if (getDiagramEditorIdx() === idx) {
                closeDiagramEditor();
            }
            else {
                openDiagramEditor(idx);
            }
            _rerender();
        });
    });
    // Diagram apply/cancel
    contentEl.querySelectorAll('.doc-diagram-apply-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const idx = parseInt(btn.dataset.idx || '0', 10);
            if (_renderedData && _renderedData.sections) {
                const rawDoc = getRawDoc();
                applyDiagramEditor(idx, _renderedData.sections, rawDoc?.sections || [], () => setDirty(true));
            }
            _rerender();
        });
    });
    contentEl.querySelectorAll('.doc-diagram-cancel-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            closeDiagramEditor();
            _rerender();
        });
    });
    // Apply buttons (read content from Tiptap)
    contentEl.querySelectorAll('.doc-apply-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const idx = parseInt(btn.dataset.idx || '0', 10);
            const editor = _tiptapEditors[idx];
            let newContent;
            if (editor) {
                newContent = editor.getHTML();
                editor.destroy();
                delete _tiptapEditors[idx];
            }
            else {
                newContent = '';
            }
            if (_renderedData && _renderedData.sections && _renderedData.sections[idx]) {
                _renderedData.sections[idx].content = newContent;
            }
            const rawDoc = getRawDoc();
            if (rawDoc && rawDoc.sections && rawDoc.sections[idx]) {
                rawDoc.sections[idx].content = newContent;
            }
            setDirty(true);
            _editingSectionIdx = null;
            _rerender();
        });
    });
    // Save button
    const saveBtn = contentEl.querySelector('#doc-save-btn');
    if (saveBtn) {
        saveBtn.addEventListener('click', () => {
            if (isDirty())
                save();
        });
    }
    // Source toggle
    const srcBtn = contentEl.querySelector('#doc-source-toggle');
    if (srcBtn) {
        srcBtn.addEventListener('click', () => {
            toggleRawSource();
        });
    }
    // Wide-mode toggle
    const wideBtn = contentEl.querySelector('#doc-wide-toggle');
    if (wideBtn) {
        wideBtn.addEventListener('click', () => {
            const goWide = !isWide();
            setWide(goWide);
            const inner = contentEl.querySelector('.doc-content-inner');
            if (inner)
                inner.classList.toggle('wide', goWide);
            wideBtn.classList.toggle('active', goWide);
            wideBtn.textContent = goWide ? '\u21E4 Narrow' : '\u21E5 Wide';
        });
    }
    // Double-click to edit content
    contentEl.querySelectorAll('.doc-section-content').forEach(el => {
        const section = el.closest('.doc-section');
        if (!section)
            return;
        el.addEventListener('dblclick', () => {
            const idx = parseInt(section.dataset.idx || '0', 10);
            const sec = _renderedData?.sections?.[idx];
            if (sec && isMermaidSection(sec.content)) {
                destroyEditors(false);
                _editingSectionIdx = null;
                openDiagramEditor(idx);
            }
            else {
                destroyDiagramEditor();
                destroyEditors(false);
                _editingSectionIdx = idx;
            }
            _rerender();
        });
    });
    // Heading editing
    contentEl.querySelectorAll('.doc-section-heading').forEach(el => {
        el.addEventListener('click', () => {
            const idx = parseInt(el.dataset.idx || '0', 10);
            _editingHeadingIdx = idx;
            _rerender();
            requestAnimationFrame(() => {
                const inp = document.querySelector(`.doc-heading-input[data-idx="${idx}"]`);
                if (inp) {
                    inp.focus();
                    inp.select();
                }
            });
        });
    });
    contentEl.querySelectorAll('.doc-heading-save-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const idx = parseInt(btn.dataset.idx || '0', 10);
            const inp = contentEl.querySelector(`.doc-heading-input[data-idx="${idx}"]`);
            if (!inp)
                return;
            const newHeading = inp.value.trim();
            if (!newHeading)
                return;
            if (_renderedData && _renderedData.sections && _renderedData.sections[idx]) {
                _renderedData.sections[idx].heading = newHeading;
            }
            const rawDoc = getRawDoc();
            if (rawDoc && rawDoc.sections && rawDoc.sections[idx]) {
                rawDoc.sections[idx].heading = newHeading;
            }
            // Auto-update slug if auto
            const sec = _renderedData?.sections?.[idx];
            if (sec && sec.slug_auto) {
                const newLocal = slugify(newHeading);
                const oldFullId = sec.id;
                const newFullId = replaceLocalSlug(sec.id, newLocal);
                if (newFullId !== oldFullId) {
                    addPendingRename(oldFullId, newFullId);
                }
                sec.id = newFullId;
                if (rawDoc && rawDoc.sections && rawDoc.sections[idx]) {
                    rawDoc.sections[idx].id = newLocal;
                }
            }
            setDirty(true);
            _editingHeadingIdx = null;
            _rerender();
        });
    });
    contentEl.querySelectorAll('.doc-heading-cancel-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            _editingHeadingIdx = null;
            _rerender();
        });
    });
    contentEl.querySelectorAll('.doc-heading-input').forEach(inp => {
        inp.addEventListener('keydown', (e) => {
            const ke = e;
            if (ke.key === 'Enter') {
                ke.preventDefault();
                const idx = parseInt(inp.dataset.idx || '0', 10);
                contentEl.querySelector(`.doc-heading-save-btn[data-idx="${idx}"]`)?.click();
            }
            else if (ke.key === 'Escape') {
                _editingHeadingIdx = null;
                _rerender();
            }
        });
    });
    // Slug editing
    contentEl.querySelectorAll('.doc-slug-text').forEach(el => {
        el.addEventListener('click', () => {
            const idx = parseInt(el.dataset.idx || '0', 10);
            _editingSlugIdx = idx;
            _rerender();
            requestAnimationFrame(() => {
                const inp = document.querySelector(`.doc-slug-input[data-idx="${idx}"]`);
                if (inp) {
                    inp.focus();
                    inp.select();
                }
            });
        });
    });
    contentEl.querySelectorAll('.doc-slug-save-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const idx = parseInt(btn.dataset.idx || '0', 10);
            const inp = contentEl.querySelector(`.doc-slug-input[data-idx="${idx}"]`);
            if (!inp)
                return;
            const newLocal = slugify(inp.value);
            if (!newLocal)
                return;
            const sections = _renderedData?.sections;
            if (!sections)
                return;
            const currentSec = sections[idx];
            const duplicate = sections.some((s, i) => i !== idx && localSlug(s.id) === newLocal && s.parent_id === currentSec.parent_id);
            if (duplicate) {
                const conflictEl = contentEl.querySelector(`.doc-slug-conflict[data-idx="${idx}"]`);
                if (conflictEl) {
                    conflictEl.textContent = '\u26A0 "' + newLocal + '" already exists';
                    conflictEl.style.color = '#f97583';
                }
                return;
            }
            const newFullId = replaceLocalSlug(currentSec.id, newLocal);
            if (newFullId !== currentSec.id) {
                addPendingRename(currentSec.id, newFullId);
            }
            sections[idx].id = newFullId;
            sections[idx].slug_auto = false;
            const rawDoc = getRawDoc();
            if (rawDoc && rawDoc.sections && rawDoc.sections[idx]) {
                rawDoc.sections[idx].id = newLocal;
                rawDoc.sections[idx].slug_auto = false;
            }
            setDirty(true);
            _editingSlugIdx = null;
            _rerender();
        });
    });
    contentEl.querySelectorAll('.doc-slug-cancel-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            _editingSlugIdx = null;
            _rerender();
        });
    });
    contentEl.querySelectorAll('.doc-slug-input').forEach(inp => {
        inp.addEventListener('input', () => {
            const idx = parseInt(inp.dataset.idx || '0', 10);
            const testSlug = slugify(inp.value);
            const sections = _renderedData?.sections;
            if (!sections)
                return;
            const currentSec = sections[idx];
            const duplicate = sections.some((s, i) => i !== idx && localSlug(s.id) === testSlug && s.parent_id === currentSec.parent_id);
            const conflictEl = contentEl.querySelector(`.doc-slug-conflict[data-idx="${idx}"]`);
            if (conflictEl) {
                if (duplicate) {
                    conflictEl.textContent = '\u26A0 "' + testSlug + '" already exists';
                    conflictEl.style.color = '#f97583';
                }
                else {
                    conflictEl.textContent = '\u2713 No conflicts';
                    conflictEl.style.color = '#a6e3a1';
                }
            }
        });
        inp.addEventListener('keydown', (e) => {
            const ke = e;
            if (ke.key === 'Enter') {
                ke.preventDefault();
                const idx = parseInt(inp.dataset.idx || '0', 10);
                contentEl.querySelector(`.doc-slug-save-btn[data-idx="${idx}"]`)?.click();
            }
            else if (ke.key === 'Escape') {
                _editingSlugIdx = null;
                _rerender();
            }
        });
    });
    // Section move up/down
    contentEl.querySelectorAll('.doc-move-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const idx = parseInt(btn.dataset.idx || '0', 10);
            const dir = btn.dataset.dir === 'up' ? -1 : 1;
            moveSection(idx, dir);
            _rerender();
        });
    });
    // Remove section
    contentEl.querySelectorAll('.doc-remove-section-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const idx = parseInt(btn.dataset.idx || '0', 10);
            const heading = _renderedData?.sections?.[idx]?.heading || `Section ${idx + 1}`;
            if (!confirm(`Remove section "${heading}"? This can be undone by not saving.`))
                return;
            removeSection(idx);
            _rerender();
        });
    });
    // Collapse toggles
    contentEl.querySelectorAll('.doc-collapse-toggle').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            toggleSectionCollapse(btn.dataset.sectionId || '');
            _rerender();
        });
    });
    // Subsection accordion toggles
    contentEl.querySelectorAll('.sub-row-header').forEach(el => {
        el.addEventListener('click', (e) => {
            // Don't toggle accordion when clicking action buttons in header
            if (e.target.closest('.sub-header-actions'))
                return;
            toggleSubsectionExpand(el.dataset.sectionId || '');
            _rerender();
        });
    });
    // Subsection compact footer: +tag click → inline input
    contentEl.querySelectorAll('.sub-tag-add').forEach(el => {
        el.addEventListener('click', (e) => {
            e.stopPropagation();
            const span = el;
            const idx = parseInt(span.dataset.idx || '0', 10);
            const input = document.createElement('input');
            input.type = 'text';
            input.className = 'sub-tag-add-input';
            input.placeholder = 'tag';
            input.maxLength = 40;
            input.spellcheck = false;
            span.replaceWith(input);
            input.focus();
            const commit = () => {
                const tag = input.value.trim();
                if (tag) {
                    addSectionTag(idx, tag);
                }
                _rerender();
            };
            input.addEventListener('keydown', (ke) => {
                if (ke.key === 'Enter') {
                    ke.preventDefault();
                    commit();
                }
                if (ke.key === 'Escape') {
                    _rerender();
                }
            });
            input.addEventListener('blur', commit);
        });
    });
    // Subsection compact footer: +link click → open link search panel
    contentEl.querySelectorAll('.sub-link-add').forEach(el => {
        el.addEventListener('click', (e) => {
            e.stopPropagation();
            const idx = parseInt(el.dataset.idx || '0', 10);
            _linkSearchIdx = (_linkSearchIdx === idx) ? null : idx;
            _linkSearchResults = [];
            _rerender();
            if (_linkSearchIdx !== null) {
                requestAnimationFrame(() => {
                    const inp = contentEl.querySelector(`.doc-link-search-input[data-idx="${idx}"]`);
                    if (inp)
                        inp.focus();
                });
            }
        });
    });
    // Double-click on subsection content to edit
    contentEl.querySelectorAll('.sub-row .doc-section-content').forEach(el => {
        const row = el.closest('.sub-row');
        if (!row)
            return;
        el.addEventListener('dblclick', () => {
            const idx = parseInt(row.dataset.idx || '0', 10);
            const sec = _renderedData?.sections?.[idx];
            if (sec && isMermaidSection(sec.content)) {
                destroyEditors(false);
                _editingSectionIdx = null;
                openDiagramEditor(idx);
            }
            else {
                destroyDiagramEditor();
                destroyEditors(false);
                _editingSectionIdx = idx;
            }
            _rerender();
        });
    });
    // Add section / sub-section
    const addSectionBtn = contentEl.querySelector('#doc-add-section-btn');
    if (addSectionBtn) {
        addSectionBtn.addEventListener('click', () => {
            addSection();
            _rerender();
        });
    }
    contentEl.querySelectorAll('.doc-add-subsection-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            addSubSection(parseInt(btn.dataset.idx || '0', 10));
            _rerender();
        });
    });
    // Tag removal
    contentEl.querySelectorAll('.doc-tag-remove').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const scope = btn.dataset.scope;
            const tagIdx = parseInt(btn.dataset.tagIdx || '0', 10);
            if (scope === 'doc') {
                removeDocTag(tagIdx);
            }
            else if (scope === 'section') {
                const secIdx = parseInt(btn.dataset.sectionIdx || '0', 10);
                removeSectionTag(secIdx, tagIdx);
            }
            _rerender();
        });
    });
    // Doc-level tag add
    const docTagInput = contentEl.querySelector('#doc-tag-add-input');
    if (docTagInput) {
        docTagInput.addEventListener('keydown', (e) => {
            const ke = e;
            if (ke.key === 'Enter') {
                ke.preventDefault();
                const tag = docTagInput.value.trim();
                if (tag) {
                    addDocTag(tag);
                    _rerender();
                }
            }
        });
    }
    // Section-level tag add
    contentEl.querySelectorAll('.doc-stag-add-input').forEach(inp => {
        inp.addEventListener('keydown', (e) => {
            const ke = e;
            if (ke.key === 'Enter') {
                ke.preventDefault();
                const idx = parseInt(inp.dataset.idx || '0', 10);
                const tag = inp.value.trim();
                if (tag) {
                    addSectionTag(idx, tag);
                    _rerender();
                }
            }
        });
    });
    // Link add button
    contentEl.querySelectorAll('.doc-link-add-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const idx = parseInt(btn.dataset.idx || '0', 10);
            _linkSearchIdx = (_linkSearchIdx === idx) ? null : idx;
            _linkSearchResults = [];
            _rerender();
            if (_linkSearchIdx !== null) {
                requestAnimationFrame(() => {
                    const inp = contentEl.querySelector(`.doc-link-search-input[data-idx="${idx}"]`);
                    if (inp)
                        inp.focus();
                });
            }
        });
    });
    // Link search input
    contentEl.querySelectorAll('.doc-link-search-input').forEach(inp => {
        inp.addEventListener('input', () => {
            const idx = parseInt(inp.dataset.idx || '0', 10);
            const query = inp.value.trim();
            if (_linkSearchTimer)
                clearTimeout(_linkSearchTimer);
            _linkSearchTimer = setTimeout(async () => {
                await doLinkSearch(query, idx);
                // Re-render just the results area
                const resultsEl = contentEl.querySelector(`.doc-link-search-results[data-idx="${idx}"]`);
                if (resultsEl) {
                    const rawDoc = getRawDoc();
                    const secLinks = rawDoc?.sections?.[idx]?.links || [];
                    resultsEl.innerHTML = _renderLinkSearchResults(idx, secLinks);
                    // Wire click handlers on results
                    resultsEl.querySelectorAll('.doc-link-search-item:not(.already-linked)').forEach(item => {
                        item.addEventListener('click', () => {
                            const nodeId = item.dataset.nodeId || '';
                            addLink(idx, nodeId);
                            _rerender();
                        });
                    });
                }
            }, 250);
        });
    });
    // Link search close
    contentEl.querySelectorAll('.doc-link-search-close').forEach(btn => {
        btn.addEventListener('click', () => {
            _linkSearchIdx = null;
            _linkSearchResults = [];
            _rerender();
        });
    });
    // Link search result clicks
    contentEl.querySelectorAll('.doc-link-search-item:not(.already-linked)').forEach(item => {
        item.addEventListener('click', () => {
            const secIdx = parseInt(item.dataset.sectionIdx || '0', 10);
            const nodeId = item.dataset.nodeId || '';
            addLink(secIdx, nodeId);
            _rerender();
        });
    });
    // Link remove
    contentEl.querySelectorAll('.doc-link-remove').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const secIdx = parseInt(btn.dataset.sectionIdx || '0', 10);
            const linkIdx = parseInt(btn.dataset.linkIdx || '0', 10);
            removeLink(secIdx, linkIdx);
            _rerender();
        });
    });
    // TOC item clicks
    contentEl.querySelectorAll('.doc-toc-item').forEach(el => {
        el.addEventListener('click', (e) => {
            e.preventDefault();
            const idx = el.dataset.sectionIdx;
            const target = contentEl.querySelector(`.doc-section[data-idx="${idx}"]`);
            if (target)
                target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        });
    });
}
