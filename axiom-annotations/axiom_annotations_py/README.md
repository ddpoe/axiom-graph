# axiom-annotations

Zero-dependency Python package providing the shared annotation vocabulary used by
[axiom-graph](https://github.com/ddpoe/axiom-graph).

Contains:

- `@workflow` and `@task` decorators
- `Step()` and `AutoStep()` inline markers
- `StepValidationError` exception
- Per-marker argument validation helpers

All markers are pure Python with **no third-party runtime dependencies**. This lets
downstream tools annotate their code with workflow/task/Step/AutoStep markers
without pulling in any heavier optional runtime stack.

## Install

```bash
pip install axiom-annotations
```

## Usage

```python
from axiom_annotations import workflow, task, Step, AutoStep

@task(purpose="Load data from file")
def load_data(path: str):
    ...

@workflow(purpose="Run the analysis pipeline")
def run_pipeline(path: str):
    ax = AutoStep(step_num=1)
    data = load_data(path)

    ax = Step(step_num=2, name="Filter", purpose="Remove bad rows")
    data = [r for r in data if r.quality > 0.5]
```

## Registry hook

By default `@workflow` and `@task` just validate and pass the function through.
Tools that want to collect decorated functions into a registry can install a
hook:

```python
from axiom_annotations.decorators import set_register_hook

def my_register(func, *, purpose, kind, inputs, outputs, critical):
    ...  # persist to your own registry

set_register_hook(my_register)
```

axiom-graph installs such a hook during scanning to populate its index.
