// =============================================================================
// doc-diagrams.ts -- Mermaid + Monaco diagram editor
//
// Handles Mermaid diagram rendering, live preview, and Monaco editing
// of diagram source within doc sections.
// =============================================================================
import { esc } from './view-utils.js';
// ── State ───────────────────────────────────────────────────────────────────
let _diagramEditorIdx = null;
let _diagramMonaco = null;
let _diagramPreviewTimer = null;
let _mermaidMonacoRegistered = false;
let _mermaidSources = [];
// ── Getters ─────────────────────────────────────────────────────────────────
export function getDiagramEditorIdx() { return _diagramEditorIdx; }
export function getMermaidSources() { return _mermaidSources; }
export function setMermaidSources(sources) { _mermaidSources = sources; }
export function clearMermaidSources() { _mermaidSources = []; }
// ── Reset ───────────────────────────────────────────────────────────────────
export function resetDiagrams() {
    destroyDiagramEditor();
    _mermaidSources = [];
}
// ── Mermaid section detection ───────────────────────────────────────────────
export function isMermaidSection(content) {
    if (!content)
        return false;
    return /```mermaid/i.test(content);
}
export function extractMermaidSource(content) {
    if (!content)
        return '';
    const match = content.match(/```mermaid\s*\n([\s\S]*?)```/);
    return match ? match[1].trim() : '';
}
// ── Mermaid rendering ───────────────────────────────────────────────────────
export async function runMermaid() {
    if (typeof mermaid === 'undefined')
        return;
    // Populate mermaid elements via textContent (avoids HTML-entity escaping issues)
    document.querySelectorAll('.doc-section-content .mermaid[data-mermaid-idx]').forEach(el => {
        const idx = parseInt(el.dataset.mermaidIdx || '-1', 10);
        if (_mermaidSources && _mermaidSources[idx] != null) {
            el.textContent = _mermaidSources[idx];
        }
    });
    try {
        await mermaid.run({ querySelector: '.doc-section-content .mermaid' });
    }
    catch (e) {
        console.warn('[cortex] mermaid.run() error:', e);
    }
    // Post-process SVGs: ensure viewBox, retain explicit width for natural sizing.
    // CSS max-width:100% prevents overflow; in expanded mode CSS overrides to width:100%.
    document.querySelectorAll('.doc-section--diagram .doc-mermaid-block .mermaid').forEach(pre => {
        const svg = pre.querySelector('svg');
        if (!svg)
            return;
        // Read intrinsic dimensions before any changes
        const origW = parseFloat(svg.getAttribute('width') || svg.style.maxWidth || svg.style.width) || svg.getBBox().width;
        const origH = parseFloat(svg.getAttribute('height') || svg.style.height) || svg.getBBox().height;
        // Synthesize viewBox if missing (needed for proportional scaling in expanded mode)
        if (!svg.getAttribute('viewBox') && origW && origH) {
            svg.setAttribute('viewBox', `0 0 ${origW} ${origH}`);
        }
        // Set explicit intrinsic width so the SVG doesn't collapse to 0
        svg.setAttribute('width', String(Math.ceil(origW)));
        svg.removeAttribute('height');
        svg.style.removeProperty('max-width');
        svg.style.removeProperty('width');
        svg.style.removeProperty('height');
        svg.style.height = 'auto';
        // Clean up the wrapper -- don't force width
        pre.style.removeProperty('width');
        pre.style.removeProperty('max-width');
    });
}
// ── Diagram editor lifecycle ────────────────────────────────────────────────
export function openDiagramEditor(idx) {
    _diagramEditorIdx = idx;
}
export function closeDiagramEditor() {
    _diagramEditorIdx = null;
    destroyDiagramEditor();
}
export function applyDiagramEditor(idx, renderedSections, rawSections, setDirty) {
    if (!_diagramMonaco)
        return;
    const newSource = _diagramMonaco.getValue();
    const newContent = '```mermaid\n' + newSource + '\n```';
    if (renderedSections && renderedSections[idx]) {
        renderedSections[idx].content = newContent;
    }
    if (rawSections && rawSections[idx]) {
        rawSections[idx].content = newContent;
    }
    setDirty();
    closeDiagramEditor();
}
export function destroyDiagramEditor() {
    if (_diagramPreviewTimer) {
        clearTimeout(_diagramPreviewTimer);
        _diagramPreviewTimer = null;
    }
    if (_diagramMonaco) {
        _diagramMonaco.dispose();
        _diagramMonaco = null;
    }
}
export function mountDiagramEditor(idx, source) {
    const editorContainer = document.getElementById(`diagram-editor-${idx}`);
    const previewContainer = document.getElementById(`diagram-preview-${idx}`);
    if (!editorContainer || !previewContainer)
        return;
    registerMermaidLanguage();
    const m = window.monaco;
    if (!m)
        return;
    _diagramMonaco = m.editor.create(editorContainer, {
        value: source,
        language: 'mermaid',
        theme: 'vs-dark',
        automaticLayout: true,
        minimap: { enabled: false },
        scrollBeyondLastLine: false,
        fontSize: 12,
        lineNumbers: 'on',
        wordWrap: 'on',
    });
    // Live preview with debounce
    _diagramMonaco.onDidChangeModelContent(() => {
        if (_diagramPreviewTimer)
            clearTimeout(_diagramPreviewTimer);
        _diagramPreviewTimer = setTimeout(() => {
            const value = _diagramMonaco.getValue();
            updateDiagramPreview(previewContainer, value);
        }, 500);
    });
    // Initial preview
    updateDiagramPreview(previewContainer, source);
}
async function updateDiagramPreview(container, source) {
    if (!container || typeof mermaid === 'undefined')
        return;
    if (!source || !source.trim()) {
        container.innerHTML = '<div style="color:#6e7681;padding:12px">Enter diagram source above</div>';
        return;
    }
    try {
        const id = `diagram-preview-${Date.now()}`;
        const { svg } = await mermaid.render(id, source);
        container.innerHTML = svg || '';
    }
    catch (err) {
        container.innerHTML = `<div style="color:#f85149;padding:8px;font-size:11px">${esc(err.message || 'Render error')}</div>`;
    }
}
// ── Monaco mermaid language registration ────────────────────────────────────
function registerMermaidLanguage() {
    if (_mermaidMonacoRegistered)
        return;
    const m = window.monaco;
    if (!m)
        return;
    _mermaidMonacoRegistered = true;
    m.languages.register({ id: 'mermaid' });
    m.languages.setMonarchTokensProvider('mermaid', {
        tokenizer: {
            root: [
                [/%%.*$/, 'comment'],
                [/\b(graph|subgraph|end|flowchart|sequenceDiagram|classDiagram|stateDiagram|erDiagram|gantt|pie|gitGraph|journey|mindmap|timeline|quadrantChart|sankey|xychart|block)\b/, 'keyword'],
                [/\b(participant|actor|activate|deactivate|Note|over|loop|alt|else|opt|par|critical|break|rect|class|state|entity|section|title|dateFormat|axisFormat|excludes|includes)\b/, 'keyword'],
                [/\b(LR|RL|TB|BT|TD)\b/, 'type'],
                [/-->|---|-\.-|==>|-.->|--x|--o|\|>|<\||\.\.>/, 'operator'],
                [/"[^"]*"/, 'string'],
                [/'[^']*'/, 'string'],
                [/\[.*?\]/, 'attribute'],
                [/\(.*?\)/, 'attribute'],
                [/\{.*?\}/, 'attribute'],
                [/:::|:::/, 'delimiter'],
                [/[a-zA-Z_]\w*/, 'identifier'],
                [/[{}()\[\]]/, 'delimiter.bracket'],
            ],
        },
    });
    // Basic completions for common mermaid starters and keywords
    m.languages.registerCompletionItemProvider('mermaid', {
        provideCompletionItems: (model, position) => {
            const word = model.getWordUntilPosition(position);
            const range = {
                startLineNumber: position.lineNumber,
                endLineNumber: position.lineNumber,
                startColumn: word.startColumn,
                endColumn: word.endColumn,
            };
            const suggestions = [
                'graph LR', 'graph TD', 'flowchart LR', 'flowchart TD',
                'sequenceDiagram', 'classDiagram', 'stateDiagram-v2',
                'erDiagram', 'gantt', 'pie', 'gitGraph', 'mindmap', 'timeline',
                'subgraph', 'end', 'participant', 'actor',
                'note over', 'note left of', 'note right of',
                'loop', 'alt', 'else', 'opt', 'par', 'critical', 'break',
                'classDef', 'style', 'click',
                'direction LR', 'direction TB',
            ].map((label) => ({
                label,
                kind: m.languages.CompletionItemKind.Keyword,
                insertText: label,
                range,
            }));
            return { suggestions };
        },
    });
}
