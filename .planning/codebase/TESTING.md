# Testing Patterns

**Analysis Date:** 2026-01-18

## Test Framework

**Status: No Test Suite Implemented**

This codebase does not currently have automated tests. There is no testing framework configured, no test files in the main codebase, and no testing dependencies in `pyproject.toml`.

**Recommended Framework:**
- pytest (standard for Python projects)

**Config File:** Not present - would be `pyproject.toml` or `pytest.ini`

**Run Commands (if tests existed):**
```bash
pytest                    # Run all tests
pytest -v                 # Verbose output
pytest --cov=raccoon      # With coverage
pytest -k "test_codegen"  # Filter by name
```

## Test File Organization

**Current State:** No test files exist in `raccoon/` directory.

**Recommended Pattern:**
```
raccoon/
├── codegen/
│   ├── __init__.py
│   ├── pipeline.py
│   └── tests/
│       ├── __init__.py
│       ├── test_pipeline.py
│       └── test_generators.py
├── client/
│   ├── __init__.py
│   ├── api.py
│   └── tests/
│       ├── test_api.py
│       └── test_connection.py
└── tests/               # Integration tests
    ├── __init__.py
    └── test_cli.py
```

**Alternative (separate tests directory):**
```
tests/
├── conftest.py          # Shared fixtures
├── test_codegen/
│   ├── test_pipeline.py
│   └── test_generators.py
├── test_client/
│   ├── test_api.py
│   └── test_connection.py
└── test_cli.py
```

## What Should Be Tested

**High-Value Test Targets:**

1. **Code Generation Pipeline** (`raccoon/codegen/`)
   - `pipeline.py`: `CodegenPipeline.run_all()` with various configs
   - `generators/base.py`: `BaseGenerator.write()` with caching
   - `generators/robot_generator.py`: kinematics/drive/odometry generation
   - `generators/defs_generator.py`: hardware definitions generation
   - `builder.py`: `build_constructor_expr()`, `build_literal_expr()`
   - `introspection.py`: `get_init_params()`, `parse_pybind11_signature()`

2. **Project Utilities** (`raccoon/project.py`)
   - `find_project_root()`: directory traversal logic
   - `load_project_config()`: YAML parsing, error handling

3. **SFTP Sync** (`raccoon/client/sftp_sync.py`)
   - `SftpSync.sync()`: file change detection
   - `_should_exclude()`: pattern matching
   - `_hash_file()`: deterministic hashing

4. **Connection Management** (`raccoon/client/connection.py`)
   - `ConnectionManager.connect()`: state transitions
   - Configuration save/load

5. **Server Routes** (`raccoon/server/routes/`)
   - API endpoint response formats
   - Authentication middleware

## Recommended Test Structure

**Unit Test Pattern:**
```python
"""Tests for raccoon.codegen.pipeline module."""

import pytest
from pathlib import Path

from raccoon.codegen.pipeline import CodegenPipeline, create_pipeline


class TestCodegenPipeline:
    """Tests for CodegenPipeline class."""

    def test_create_pipeline_returns_configured_instance(self):
        """Factory function should return a CodegenPipeline with default generators."""
        pipeline = create_pipeline()

        assert isinstance(pipeline, CodegenPipeline)
        assert "defs" in pipeline.list_generators()
        assert "robot" in pipeline.list_generators()

    def test_run_all_generates_expected_files(self, tmp_path: Path):
        """run_all should generate defs.py and robot.py."""
        pipeline = create_pipeline()
        config = {
            "name": "TestProject",
            "definitions": {"motor1": {"type": "Motor", "port": 0}},
            "robot": {"kinematics": {"type": "differential", "wheel_radius": 0.05}},
        }

        results = pipeline.run_all(config, tmp_path, format_code=False)

        assert "defs" in results
        assert "robot" in results
        assert (tmp_path / "defs.py").exists()
        assert (tmp_path / "robot.py").exists()
```

**Integration Test Pattern:**
```python
"""Integration tests for raccoon CLI."""

import subprocess
from pathlib import Path


def test_create_project_generates_valid_structure(tmp_path: Path):
    """raccoon create project should generate a valid project structure."""
    result = subprocess.run(
        ["raccoon", "create", "project", "test-proj", "--no-wizard", "--path", str(tmp_path)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    project_dir = tmp_path / "test-proj"
    assert project_dir.exists()
    assert (project_dir / "raccoon.project.yml").exists()
    assert (project_dir / "src" / "main.py").exists()
```

## Mocking Patterns

**Framework:** `unittest.mock` or `pytest-mock`

**What to Mock:**
- SSH connections (`paramiko.SSHClient`)
- HTTP clients (`httpx.AsyncClient`)
- File system operations for isolation
- External `libstp` library (not always available)

**What NOT to Mock:**
- Code generation logic (test actual output)
- YAML parsing (test real configs)
- Path manipulation

**Mock Example:**
```python
from unittest.mock import Mock, patch

def test_fetch_api_token_handles_ssh_failure():
    """Should return None when SSH connection fails."""
    manager = ConnectionManager()

    with patch("paramiko.SSHClient") as mock_ssh:
        mock_ssh.return_value.connect.side_effect = Exception("Connection refused")

        result = manager._fetch_api_token_via_ssh("192.168.1.1", "pi")

        assert result is None
```

**Async Mocking:**
```python
import pytest
from unittest.mock import AsyncMock

@pytest.mark.asyncio
async def test_health_check_returns_status():
    """API client health check should return server status."""
    with patch("httpx.AsyncClient") as mock_client:
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "ok", "version": "1.0.0"}
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_response)

        async with RaccoonApiClient("http://localhost:8421") as client:
            result = await client.health()

        assert result["status"] == "ok"
```

## Fixtures

**Recommended Fixtures for `conftest.py`:**
```python
import pytest
from pathlib import Path
import yaml


@pytest.fixture
def sample_project_config() -> dict:
    """Minimal valid project configuration."""
    return {
        "name": "TestProject",
        "uuid": "test-uuid-1234",
        "definitions": {},
        "robot": {},
        "missions": [],
    }


@pytest.fixture
def project_dir(tmp_path: Path, sample_project_config: dict) -> Path:
    """Temporary project directory with raccoon.project.yml."""
    project = tmp_path / "test-project"
    project.mkdir()

    config_file = project / "raccoon.project.yml"
    with open(config_file, "w") as f:
        yaml.safe_dump(sample_project_config, f)

    # Create src structure
    (project / "src").mkdir()
    (project / "src" / "hardware").mkdir()
    (project / "src" / "missions").mkdir()

    return project


@pytest.fixture
def mock_libstp():
    """Mock libstp module when not installed."""
    import sys
    from unittest.mock import MagicMock

    mock = MagicMock()
    mock.drive.Drive = MagicMock
    mock.kinematics.DifferentialKinematics = MagicMock

    sys.modules["libstp"] = mock
    sys.modules["libstp.drive"] = mock.drive
    sys.modules["libstp.kinematics"] = mock.kinematics

    yield mock

    del sys.modules["libstp"]
    del sys.modules["libstp.drive"]
    del sys.modules["libstp.kinematics"]
```

## Coverage

**Requirements:** None enforced (no CI pipeline detected)

**Recommended Target:** 70-80% for core modules

**View Coverage (if implemented):**
```bash
pytest --cov=raccoon --cov-report=html
open htmlcov/index.html
```

**Priority Coverage Areas:**
- `raccoon/codegen/` - Core business logic
- `raccoon/project.py` - Project utilities
- `raccoon/client/sftp_sync.py` - File sync logic

## Test Types

**Unit Tests:**
- Test individual functions and methods in isolation
- Mock external dependencies
- Fast execution (< 1s per test)
- Target: `raccoon/codegen/`, `raccoon/project.py`

**Integration Tests:**
- Test CLI commands end-to-end
- Use temporary directories for file operations
- May require network mocking for client tests
- Target: `raccoon/commands/`, full workflow tests

**E2E Tests:**
- Not applicable without actual hardware
- Manual testing with Pi required for `raccoon/server/` and `raccoon/client/`

## Common Testing Patterns

**Testing Code Generation:**
```python
def test_generator_output_is_valid_python(tmp_path: Path):
    """Generated code should be syntactically valid Python."""
    from raccoon.codegen import create_pipeline

    config = {"name": "Test", "definitions": {}, "robot": {}}
    pipeline = create_pipeline()

    results = pipeline.run_all(config, tmp_path, format_code=False)

    for name, path in results.items():
        content = path.read_text()
        # This will raise SyntaxError if invalid
        compile(content, str(path), "exec")
```

**Testing Error Handling:**
```python
def test_load_config_raises_on_invalid_yaml(tmp_path: Path):
    """Should raise ProjectError for malformed YAML."""
    config_file = tmp_path / "raccoon.project.yml"
    config_file.write_text("invalid: yaml: content:")

    with pytest.raises(ProjectError, match="Invalid YAML"):
        load_project_config(tmp_path)
```

**Testing CLI Commands:**
```python
from click.testing import CliRunner
from raccoon.cli import main

def test_status_command_shows_not_connected():
    """Status command should indicate when not connected."""
    runner = CliRunner()
    result = runner.invoke(main, ["status"])

    assert result.exit_code == 0
    assert "Not connected" in result.output
```

## Test Data

**Location:** Would typically be in `tests/fixtures/` or alongside test files

**Sample Config Files:**
```yaml
# tests/fixtures/minimal_config.yml
name: MinimalProject
uuid: test-uuid
definitions: {}
robot: {}
missions: []

# tests/fixtures/full_config.yml
name: FullProject
uuid: test-uuid-full
definitions:
  left_motor:
    type: Motor
    port: 0
    inverted: false
robot:
  drive:
    kinematics:
      type: differential
      left_motor: left_motor
      right_motor: right_motor
      wheel_radius: 0.05
      wheel_base: 0.15
missions:
  - SetupMission: setup
  - MainMission: normal
  - ShutdownMission: shutdown
```

## Continuous Integration

**Status:** No CI configuration detected

**Recommended Setup (GitHub Actions):**
```yaml
# .github/workflows/test.yml
name: Tests
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -e ".[test]"
      - run: pytest --cov=raccoon
```

---

*Testing analysis: 2026-01-18*
