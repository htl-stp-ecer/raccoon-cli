# Coding Conventions

**Analysis Date:** 2026-01-18

## Naming Patterns

**Files:**
- snake_case for all Python modules: `robot_generator.py`, `sftp_sync.py`, `output_handler.py`
- Commands avoid Python reserved words with `_cmd` suffix: `list_cmd.py`, `sync_cmd.py`, `remove_cmd.py`
- Double underscore for package entry points: `__init__.py`, `__main__.py`

**Functions:**
- snake_case for all functions: `find_project_root()`, `load_project_config()`, `get_init_params()`
- Private functions prefixed with underscore: `_setup_context()`, `_run_local()`, `_build_kinematics()`
- Factory functions use `create_` prefix: `create_pipeline()`, `create_api_client()`, `create_hardware_resolver()`

**Variables:**
- snake_case for all variables: `project_root`, `config_path`, `exit_code`
- Constants in UPPER_CASE: `CONTEXT_SETTINGS`, `HARDWARE_REF_PARAMS`, `CACHE_SCHEMA_VERSION`
- Private class attributes with underscore prefix: `_state`, `_client`, `_full_config`

**Classes:**
- PascalCase for all classes: `ProjectError`, `CodegenPipeline`, `ConnectionManager`
- Base/Abstract classes prefixed with `Base`: `BaseGenerator`
- Data classes use descriptive names: `SyncResult`, `CommandResult`, `EncoderReading`
- Builders suffixed with `Builder`: `ClassBuilder`, `ImportSet`

**Types:**
- Type aliases defined in module scope (rarely used in this codebase)
- Prefer explicit type hints over aliases

## Code Style

**Formatting:**
- Tool: Black (included in dependencies)
- Line length: 88 characters
- String quotes: Double quotes for docstrings, both single and double used elsewhere

**Linting:**
- No explicit linting config files found (`.eslintrc`, `pyproject.toml [tool.ruff]`, etc.)
- Implicit standards followed but not enforced by tooling

## Import Organization

**Order:**
1. `from __future__ import annotations` (when needed for type hints)
2. Standard library imports (`os`, `logging`, `asyncio`, `pathlib`, etc.)
3. Third-party imports (`click`, `rich`, `fastapi`, `pydantic`, `httpx`)
4. Local package imports (`from raccoon.codegen import ...`)
5. Relative imports within the same package (`from .base import BaseGenerator`)

**Example from `raccoon/commands/run.py`:**
```python
from __future__ import annotations

import asyncio
import logging
import signal
import subprocess
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from raccoon.codegen import create_pipeline
from raccoon.project import ProjectError, load_project_config, require_project
```

**Path Aliases:**
- None configured. Always use full relative or absolute imports.

## Error Handling

**Custom Exceptions:**
- Define domain-specific exceptions inheriting from `Exception`
- Location: `raccoon/project.py` defines `ProjectError`

```python
class ProjectError(Exception):
    """Raised when project validation fails."""
    pass
```

**Patterns:**
- Raise `ProjectError` for user-facing configuration/project errors
- Raise `ValueError` for invalid function arguments in library code
- Raise `RuntimeError` for internal state errors
- Use `SystemExit(1)` in CLI commands to indicate failure

**Error Messages:**
- Include actionable guidance in error messages
- Multi-line messages with `\n` for complex errors

```python
raise ProjectError(
    f"Not in a project directory. No raccoon.project.yml found.\n"
    "Create a raccoon.project.yml file in your project root."
)
```

**Try-Except Blocks:**
- Specific exception types over bare `except`
- Log exceptions with context before re-raising
- Use `raise ... from exc` for exception chaining

```python
except ProjectError as exc:
    logger.error(str(exc))
    raise SystemExit(1) from exc
except SystemExit:
    raise
except Exception:
    logger.exception("Unexpected error while running project")
    raise SystemExit(1) from None
```

## Logging

**Framework:** Python `logging` module with Rich integration

**Logger Setup:**
```python
import logging
logger = logging.getLogger("raccoon")
```

**Patterns:**
- One logger per module, always named `"raccoon"`
- Use `logger.info()` for important progress messages
- Use `logger.warning()` for recoverable issues
- Use `logger.error()` for failures before raising
- Use `logger.debug()` for verbose diagnostic output
- Use `logger.exception()` to include traceback

**Rich Integration (CLI):**
- Console output via `rich.console.Console`
- Styled messages: `console.print("[cyan]Message...[/cyan]")`
- Progress bars via `rich.progress.Progress`
- Panels for status display: `rich.panel.Panel`

## Comments

**When to Comment:**
- Module-level docstrings for all modules
- Class docstrings explaining purpose
- Function docstrings for public functions with Args/Returns/Raises
- Inline comments for non-obvious logic only

**Docstring Style:**
```python
def find_project_root(start_path: Path | None = None) -> Path:
    """
    Find the project root by looking for raccoon.project.yml.

    Searches upward from start_path (default: current directory) until
    finding raccoon.project.yml or hitting the filesystem root.

    Returns:
        Path to the directory containing raccoon.project.yml

    Raises:
        ProjectError: If no raccoon.project.yml is found
    """
```

**File Headers:**
- Brief one-line docstring for every module
- Example: `"""Run command for raccoon CLI."""`

## Function Design

**Size:**
- Functions aim for single responsibility
- Complex functions are broken into private helper methods
- Long functions acceptable in code generation context

**Parameters:**
- Use type hints for all parameters
- Use `Optional[T]` or `T | None` for nullable parameters
- Default arguments for optional configuration
- Use `*` to force keyword-only arguments in complex signatures

**Return Values:**
- Always type-hinted
- Return early on error conditions
- Prefer returning data classes over tuples for complex returns

```python
@dataclass
class SyncResult:
    success: bool
    files_uploaded: int = 0
    files_deleted: int = 0
    bytes_transferred: int = 0
    errors: list[str] = field(default_factory=list)
```

## Module Design

**Exports:**
- Use `__all__` to declare public API in `__init__.py`
- Example from `raccoon/codegen/__init__.py`:

```python
__all__ = [
    'resolve_class',
    'get_init_params',
    'CodegenPipeline',
    'create_pipeline',
    'BaseGenerator',
    # ...
]
```

**Barrel Files:**
- `__init__.py` files re-export commonly used items
- Commands module uses barrel pattern: `raccoon/commands/__init__.py`

## Class Design Patterns

**Dataclasses:**
- Use `@dataclass` for pure data containers
- Use `field(default_factory=...)` for mutable defaults

```python
@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8421
    projects_dir: Path = field(default_factory=lambda: Path.home() / "programs")
```

**Builder Pattern:**
- Used for constructing complex objects: `ClassBuilder`, `ImportSet`
- Return `self` for method chaining

```python
def add_class_attribute(self, name: str, expression: str) -> ClassBuilder:
    self._class_attrs.append((name, expression))
    return self
```

**Template Method Pattern:**
- `BaseGenerator` uses template method for code generation workflow
- Subclasses override `extract_config()`, `validate_config()`, `generate_body()`

**Singleton Pattern:**
- Global manager instances with getter functions: `get_connection_manager()`

```python
_connection_manager: Optional[ConnectionManager] = None

def get_connection_manager() -> ConnectionManager:
    global _connection_manager
    if _connection_manager is None:
        _connection_manager = ConnectionManager()
    return _connection_manager
```

## Async Patterns

**Async Context Managers:**
- Use `async with` for resource management
- Implement `__aenter__` and `__aexit__` for clients

```python
async def __aenter__(self):
    self._client = httpx.AsyncClient(timeout=self.timeout)
    return self

async def __aexit__(self, exc_type, exc_val, exc_tb):
    if self._client:
        await self._client.aclose()
```

**Sync Wrappers:**
- Provide `_sync` suffix methods wrapping async calls

```python
def connect_sync(self, address: str, port: int = 8421) -> bool:
    return asyncio.run(self.connect(address, port))
```

## CLI Conventions (Click)

**Command Structure:**
- Use `@click.command()` or `@click.group()` decorators
- Pass context with `@click.pass_context`
- Access shared state via `ctx.obj["console"]`

**Naming:**
- Commands use snake_case in code: `run_command`, `create_command`
- CLI names use kebab-case via `name=`: `@click.command(name="run")`

**Options and Arguments:**
- Use `--long-name` and `-s` short form
- Include `help=` for all options
- Use `is_flag=True` for boolean flags

```python
@click.option("--local", "-l", is_flag=True, help="Force local execution (skip remote)")
```

---

*Convention analysis: 2026-01-18*
