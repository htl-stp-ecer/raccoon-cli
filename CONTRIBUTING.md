# Contributing to raccoon-cli

---

## Dev setup

raccoon-cli is a standard Python package. Install it in editable mode:

```bash
git clone https://github.com/htl-stp-ecer/raccoon-cli.git --recurse-submodules
cd raccoon-cli
pip install -e .
```

Run tests:

```bash
pytest tests/
pytest tests/test_sftp_sync.py -v   # specific file
```

Code is formatted with [Black](https://black.readthedocs.io/):

```bash
black raccoon/
```

---

## Project layout

```
raccoon/
├── cli.py                  # Click entry point -- commands registered here
├── commands/               # One file per command (or group)
│   ├── __init__.py         # Re-exports all commands
│   └── my_command.py
├── codegen/                # Code generation system
│   ├── pipeline.py         # Orchestrates all generators
│   ├── generators/
│   │   ├── base.py         # BaseGenerator abstract class
│   │   ├── registry.py     # Manages + runs generators
│   │   ├── defs_generator.py
│   │   └── robot_generator.py
│   └── builder.py          # Python AST / class builder
├── client/                 # Laptop-side: SSH, SFTP, HTTP client
│   ├── sftp_sync.py        # Hash-based file sync
│   ├── connection.py       # Connection state management
│   └── api.py              # REST API client for raccoon-server
├── server/                 # Pi-side FastAPI daemon
│   ├── app.py
│   ├── routes/
│   └── services/
└── templates/              # Jinja2 scaffolding templates
    ├── project_scaffold/   # raccoon create project
    └── mission/            # raccoon create mission
```

---

## Adding a command

### 1. Create the command file

```python
# raccoon/commands/my_command.py
import click
from rich.console import Console

@click.command(name="my-command")
@click.argument("name")
@click.pass_context
def my_command(ctx: click.Context, name: str) -> None:
    """One-line description shown in raccoon --help."""
    console: Console = ctx.obj["console"]
    console.print(f"Hello, {name}")
```

For a group of subcommands:

```python
@click.group(name="my-group")
def my_group() -> None:
    """Group description."""

@my_group.command(name="sub")
@click.pass_context
def sub_command(ctx: click.Context) -> None:
    """Subcommand description."""
    ...
```

### 2. Export from `commands/__init__.py`

```python
from .my_command import my_command
# add to __all__ too
```

### 3. Register in `cli.py`

```python
from raccoon.commands import my_command
# ...
main.add_command(my_command)
```

---

## Adding a code generator

Generators live in `codegen/generators/` and subclass `BaseGenerator`. They transform the parsed `raccoon.project.yml` into Python source files.

```python
# codegen/generators/my_generator.py
from pathlib import Path
from .base import BaseGenerator

class MyGenerator(BaseGenerator):
    def __init__(self):
        super().__init__(class_name="MyClass")

    def generate(self, config: dict, output_dir: Path) -> None:
        # build Python source from config and write to output_dir
        output = self._build_source(config)
        (output_dir / "my_file.py").write_text(output)
```

Register it in `codegen/pipeline.py`:

```python
from .generators.my_generator import MyGenerator
registry.register("my", MyGenerator())
```

---

## Adding a project template

Project scaffolding uses [Copier](https://copier.readthedocs.io/) with Jinja2 templates under `templates/project_scaffold/`.

- Add a `.jinja` file at the path where it should be created in the new project
- Use `{{ variable_name }}` for substitutions defined in `copier.yaml`
- `raccoon.project.yml.jinja` is the root config template -- edit it if you're adding new top-level config keys

---

## Client-server split

Keep laptop-only code in `client/` and Pi-only code in `server/`. Commands that need to run on the Pi should:

1. Check connection via `get_connection_manager()`
2. Call the relevant `api_client` method (REST or WebSocket)
3. Stream output back with `OutputHandler`

See `commands/run.py` for a full example.

Client and Server should use shared services & repositiories and only differ in the interfacing layer - Especially important for the web ide server and cli
