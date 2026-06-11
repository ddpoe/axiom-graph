---
name: axiom-annotations-markers
description: Reference for axiom-annotations decorator and step marker syntax. Use when adding or modifying @workflow, @task, Step, or AutoStep markers in Python code.
disable-model-invocation: false
---

# Axiom Annotation Marker Syntax Reference

The canonical reference for axiom-annotations decorator and step marker syntax lives in axiom-graph docs:

```
axiom_graph_render("axiom_graph::docs.references.axiom-annotations-markers", level=2)
```

Use `axiom_graph_search("axiom annotation step marker")` to find it, or render individual sections:

- `axiom_graph::docs.references.axiom-annotations-markers::core-rule` — the one rule that governs everything
- `axiom_graph::docs.references.axiom-annotations-markers::decorators` — `@workflow` and `@task` usage
- `axiom_graph::docs.references.axiom-annotations-markers::step-markers` — `Step` and `AutoStep` fields and numbering
- `axiom_graph::docs.references.axiom-annotations-markers::common-mistakes` — error table
- `axiom_graph::docs.references.axiom-annotations-markers::pattern-summary` — quick copy-paste template

**Quick reminder:** Minor step numbers (N.M) can ONLY appear inside loops. Major step numbers (integers) can appear anywhere.
