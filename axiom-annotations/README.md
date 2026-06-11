# axiom-annotations

Multi-language home for the shared workflow / task / Step / AutoStep annotation vocabulary used by [axiom-graph](https://github.com/ddpoe/axiom-graph).

| Language | Directory | Package name | Status |
|---|---|---|---|
| Python | [`axiom_annotations_py/`](./axiom_annotations_py) | `axiom-annotations` (PyPI) | Released — `pip install axiom-annotations` |
| TypeScript / JavaScript | [`axiom_annotations_js/`](./axiom_annotations_js) | `axiom-annotations` (npm) | In development |

Each package is independently versioned and published. They share semantics but not tooling — different package managers, different test runners, different release cadences.

The Python module name `axiom_annotations` (importable as `import axiom_annotations`) is unchanged by the directory restructure. Only the poetry project root moved.
