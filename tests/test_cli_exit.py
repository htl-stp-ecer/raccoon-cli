"""Process exit of the CLI with the robot library loaded."""

import subprocess
import sys

import pytest

# Stand-ins for the exit behaviour of the robot library
_ABORT_AT_EXIT = "import atexit, os\natexit.register(os.abort)\n"
_REPLACE_STATUS = "import atexit, os\natexit.register(os._exit, 0)\n"

_RUN = """\
import sys
from raccoon_cli import cli

def command():
    import raccoon
    print("output")
    {body}

cli.main = command
cli.entry_point()
"""


@pytest.mark.parametrize("library", [_ABORT_AT_EXIT, _REPLACE_STATUS])
@pytest.mark.parametrize(
    "body, code",
    [("sys.exit(0)", 0), ("sys.exit(2)", 2), ("raise RuntimeError('boom')", 1)],
)
def test_exit_status_survives_the_robot_library(tmp_path, library, body, code):
    (tmp_path / "raccoon.py").write_text(library)
    (tmp_path / "script.py").write_text(_RUN.format(body=body))

    result = subprocess.run(
        [sys.executable, "script.py"], cwd=tmp_path, capture_output=True, text=True, timeout=60
    )

    assert result.returncode == code, result.stderr
    assert "output" in result.stdout
    assert ("boom" in result.stderr) is ("boom" in body)
