import os
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from raccoon_cli.ide.app import create_app


PROJECT_UUID = "62df6ec4-9d0d-46bb-b8f5-b72991a3e9d1"
RUN_ROUTE = f"/api/v1/missions/{PROJECT_UUID}/run"


def test_create_mission_route_uses_shared_cli_creation_path(tmp_path: Path):
    project_uuid = "62df6ec4-9d0d-46bb-b8f5-b72991a3e9d1"
    project_dir = tmp_path / "Demo Bot"
    project_dir.mkdir()
    (project_dir / "raccoon.project.yml").write_text(
        f"name: Demo Bot\nuuid: {project_uuid}\n",
        encoding="utf-8",
    )
    (project_dir / "src").mkdir()
    (project_dir / "src" / "missions").mkdir(parents=True)

    with patch("raccoon_cli.ide.repositories.project_repository._create_mission") as mock_create:
        mock_create.return_value = "M010DriveMission"
        client = TestClient(create_app(project_root=tmp_path))
        response = client.post(f"/api/v1/missions/{project_uuid}", json={"name": "Drive"})

    assert response.status_code == 200, response.text
    mock_create.assert_called_once_with(project_dir, "Drive")


def _receive_run(websocket):
    events = []
    while True:
        try:
            events.append(websocket.receive_json())
        except WebSocketDisconnect:
            return events


@pytest.fixture
def local_project(tmp_path, monkeypatch):
    pytest.importorskip("raccoon")
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    root = Path(__file__).resolve().parent.parent
    isolated_home = tmp_path / "home"
    isolated_home.mkdir()
    monkeypatch.setenv("HOME", str(isolated_home))
    monkeypatch.setenv("USERPROFILE", str(isolated_home))
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(filter(None, [str(tmp_path), str(root), os.environ.get("PYTHONPATH")])),
    )
    # Exercise the actual CLI without scanning for and terminating robot programs
    # outside the test's temporary home.
    (tmp_path / "sitecustomize.py").write_text(
        "import sys\n"
        "if 'run' in sys.argv and '--raw' in sys.argv:\n"
        "    from raccoon_cli.commands import run\n"
        "    run._list_robot_program_pids = lambda: []\n"
    )
    (project / "raccoon.project.yml").write_text(
        f"name: Test project\nuuid: {PROJECT_UUID}\nformat_version: 2\n"
        "auto_checkpoints: false\n"
        "definitions: {button: {type: DigitalSensor, port: 10}}\n"
        "robot: {motion_pid: {}, shutdown_in: 30}\n"
        "run_configurations:\n"
        "  local-dev:\n"
        "    target: local\n"
        "    dev: true\n"
        "    no_codegen: true\n"
        "    record_localization: true\n"
        "    args: ['argument with spaces']\n"
        "    env: {IDE_TEST_VALUE: configured}\n"
    )
    return project


def test_webide_run_uses_cli_for_current_project_and_preserves_streams(tmp_path, local_project):
    (local_project / "src" / "main.py").write_text(
        "import os, sys\n"
        "from pathlib import Path\n"
        "assert os.environ['LIBSTP_DEV_MODE'] == '1'\n"
        "assert sys.argv[1:] == ['argument with spaces']\n"
        "print('stdout: ' + os.environ['IDE_TEST_VALUE'])\n"
        "print('last stderr line', file=sys.stderr)\n"
        "Path(os.environ['LIBSTP_RECORDING_PATH']).write_text('{}\\n')\n"
    )
    with TestClient(create_app(project_root=tmp_path)) as client:
        with client.websocket_connect(f"{RUN_ROUTE}?run_config=local-dev&simulate=false") as ws:
            events = _receive_run(ws)

    assert any(event["type"] == "started" for event in events)
    assert any(event["type"] == "stdout" and event["line"] == "stdout: configured" for event in events), events
    assert any(event["type"] == "stderr" and event["line"] == "last stderr line" for event in events)
    assert next(event["returncode"] for event in events if event["type"] == "exit") == 0
    run_id = next(event["run_id"] for event in events if event["type"] == "run_recorded")
    run_dir = local_project / ".raccoon" / "runs" / run_id
    assert (run_dir / "localization.jsonl").exists()
    assert (run_dir / "run.json").exists()


def test_webide_builtin_dev_without_pi_reports_cli_connection_error(tmp_path, local_project):
    (local_project / "src" / "main.py").write_text("raise AssertionError('unexpected local run')\n")
    with TestClient(create_app(project_root=tmp_path)) as client:
        with client.websocket_connect(f"{RUN_ROUTE}?run_config=dev&simulate=false") as ws:
            events = _receive_run(ws)

    assert next(event["returncode"] for event in events if event["type"] == "exit") == 1
    output = "\n".join(event.get("line", "") for event in events)
    assert "no Pi connection is available" in output
    assert "run.sh not found" not in output


@pytest.mark.skipif(os.name != "posix", reason="Verifies POSIX process group cancellation")
@pytest.mark.parametrize("explicit_stop", [True, False], ids=["stop-endpoint", "disconnect"])
def test_webide_terminates_current_local_program(tmp_path, local_project, explicit_stop):
    (local_project / "src" / "main.py").write_text(
        "import signal, time\n"
        "from pathlib import Path\n"
        "def stop(*_):\n"
        "    Path('stopped').touch()\n"
        "    raise SystemExit(15)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "print('test program running')\n"
        "time.sleep(10)\n"
    )
    with TestClient(create_app(project_root=tmp_path)) as client:
        with client.websocket_connect(f"{RUN_ROUTE}?run_config=local-dev&simulate=false") as ws:
            while True:
                event = ws.receive_json()
                assert event["type"] not in {"exit", "error"}, event
                if event.get("line") == "test program running":
                    break
            if explicit_stop:
                assert client.post(f"/api/v1/missions/{PROJECT_UUID}/stop").json()["stopped"]
                events = _receive_run(ws)
                assert next(event["returncode"] for event in events if event["type"] == "exit") != 0

    assert (local_project / "stopped").exists()


@pytest.mark.skipif(shutil.which("bash") is None, reason="Legacy launch scripts require bash")
def test_webide_preserves_legacy_run_script(tmp_path):
    project = tmp_path / "legacy"
    project.mkdir()
    (project / "raccoon.project.yml").write_text(f"name: Legacy project\nuuid: {PROJECT_UUID}\n")
    (project / "run.sh").write_text(
        'printf "legacy dev=%s\\n" "$LIBSTP_DEV_MODE"\n'
        'printf "legacy stderr\\n" >&2\n'
    )
    with TestClient(create_app(project_root=tmp_path)) as client:
        with client.websocket_connect(f"{RUN_ROUTE}?run_config=dev&simulate=false") as ws:
            events = _receive_run(ws)

    assert any(event["type"] == "stdout" and event["line"] == "legacy dev=1" for event in events)
    assert any(event["type"] == "stderr" and event["line"] == "legacy stderr" for event in events)
    assert next(event["returncode"] for event in events if event["type"] == "exit") == 0
