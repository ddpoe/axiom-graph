// =============================================================================
// monaco-facade.ts -- Typed wrapper for the AMD-loaded Monaco editor
//
// Monaco loads via AMD (not ESM), so we access it through the global
// `monaco` namespace. This facade provides typed access without pulling in
// the full @types/monaco-editor package.
// =============================================================================

/** Minimal Monaco editor interfaces -- just enough for our usage. */
export interface IMonacoEditor {
  getValue(): string;
  setValue(value: string): void;
  getModel(): any;
  dispose(): void;
  layout(dimension?: { width: number; height: number }): void;
  onDidChangeModelContent(listener: (e: any) => void): any;
  revealLineInCenter(lineNumber: number): void;
  deltaDecorations(oldDecorations: string[], newDecorations: any[]): string[];
  updateOptions(options: Record<string, any>): void;
  getAction(id: string): any;
}

export interface IMonacoDiffEditor {
  getOriginalEditor(): IMonacoEditor;
  getModifiedEditor(): IMonacoEditor;
  dispose(): void;
  layout(dimension?: { width: number; height: number }): void;
}

/** Access the global `monaco` namespace. Returns undefined if not yet loaded. */
export function getMonaco(): any {
  return (window as any).monaco;
}

/** Create a Monaco editor instance inside the given container. */
export function createEditor(
  container: HTMLElement,
  options: Record<string, any> = {},
): IMonacoEditor | null {
  const m = getMonaco();
  if (!m) return null;
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
  }) as IMonacoEditor;
}

/** Create a Monaco diff editor instance. */
export function createDiffEditor(
  container: HTMLElement,
  options: Record<string, any> = {},
): IMonacoDiffEditor | null {
  const m = getMonaco();
  if (!m) return null;
  return m.editor.createDiffEditor(container, {
    theme: 'vs-dark',
    automaticLayout: true,
    readOnly: true,
    minimap: { enabled: false },
    scrollBeyondLastLine: false,
    fontSize: 12,
    renderSideBySide: true,
    ...options,
  }) as IMonacoDiffEditor;
}

/** Create a Monaco text model. */
export function createModel(value: string, language?: string): any {
  const m = getMonaco();
  if (!m) return null;
  return m.editor.createModel(value, language || 'plaintext');
}

/** Set the language of an existing model. */
export function setModelLanguage(model: any, language: string): void {
  const m = getMonaco();
  if (m && model) {
    m.editor.setModelLanguage(model, language);
  }
}

/** Get language ID from a file path extension. */
export function languageFromPath(path: string): string {
  const ext = path.split('.').pop()?.toLowerCase() || '';
  const map: Record<string, string> = {
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
