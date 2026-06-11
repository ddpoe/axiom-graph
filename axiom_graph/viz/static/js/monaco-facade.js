// =============================================================================
// monaco-facade.ts -- Typed wrapper for the AMD-loaded Monaco editor
//
// Monaco loads via AMD (not ESM), so we access it through the global
// `monaco` namespace. This facade provides typed access without pulling in
// the full @types/monaco-editor package.
// =============================================================================
/** Access the global `monaco` namespace. Returns undefined if not yet loaded. */
export function getMonaco() {
    return window.monaco;
}
/** Create a Monaco editor instance inside the given container. */
export function createEditor(container, options = {}) {
    const m = getMonaco();
    if (!m)
        return null;
    return m.editor.create(container, {
        theme: 'vs-dark',
        automaticLayout: true,
        readOnly: true,
        minimap: { enabled: false },
        scrollBeyondLastLine: false,
        fontSize: 12,
        lineNumbers: 'on',
        renderLineHighlight: 'line',
        ...options,
    });
}
/** Create a Monaco diff editor instance. */
export function createDiffEditor(container, options = {}) {
    const m = getMonaco();
    if (!m)
        return null;
    return m.editor.createDiffEditor(container, {
        theme: 'vs-dark',
        automaticLayout: true,
        readOnly: true,
        minimap: { enabled: false },
        scrollBeyondLastLine: false,
        fontSize: 12,
        renderSideBySide: true,
        ...options,
    });
}
/** Create a Monaco text model. */
export function createModel(value, language) {
    const m = getMonaco();
    if (!m)
        return null;
    return m.editor.createModel(value, language || 'plaintext');
}
/** Set the language of an existing model. */
export function setModelLanguage(model, language) {
    const m = getMonaco();
    if (m && model) {
        m.editor.setModelLanguage(model, language);
    }
}
/** Get language ID from a file path extension. */
export function languageFromPath(path) {
    const ext = path.split('.').pop()?.toLowerCase() || '';
    const map = {
        py: 'python',
        js: 'javascript',
        ts: 'typescript',
        json: 'json',
        md: 'markdown',
        yaml: 'yaml',
        yml: 'yaml',
        toml: 'toml',
        css: 'css',
        html: 'html',
        sh: 'shell',
        bash: 'shell',
    };
    return map[ext] || 'plaintext';
}
