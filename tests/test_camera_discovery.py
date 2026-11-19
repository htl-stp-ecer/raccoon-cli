import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from raccoon_cli import camera_discovery as cd

needs_raccoon_cam = pytest.mark.skipif(
    importlib.util.find_spec("raccoon_cam") is None, reason="raccoon-cam is not installed here"
)


def write(project: Path, relative: str, text: str) -> None:
    file = project / relative
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text)


BACKEND = '''
from raccoon_cam import CamBackend, Option

class Mine(CamBackend):
    """My own camera.

    More text that the list does not show.
    """
    name = "mine"
    allow_processors = True
    provides_detections = False
    options = (Option("gain", "float", 1.0),)
    def start(self, sink): pass
    def stop(self): pass
'''


# -- scanning --------------------------------------------------------------------------------------


def test_a_backend_in_src_is_found_without_importing_it(tmp_path):
    write(tmp_path, "src/cam/mine.py", BACKEND.replace("from raccoon_cam", "from a_package_that_is_not_there"))
    [found] = cd.scan_project(tmp_path)
    assert (found.kind, found.class_name, found.module, found.path) == ("backend", "Mine", "src.cam.mine", "src.cam.mine:Mine")
    assert found.name == "mine" and found.description == "My own camera." and found.file == "src/cam/mine.py"


@pytest.mark.parametrize(
    "header, base",
    [
        ("from raccoon_cam import CamBackend", "CamBackend"),
        ("import raccoon_cam", "raccoon_cam.CamBackend"),
        ("from raccoon_cam.backends.base import CamBackend as Base", "raccoon_cam.backends.base.CamBackend"),
        ("from raccoon_cam import backends", "backends.CamBackend"),
    ],
)
def test_the_base_class_is_recognised_however_it_is_imported(tmp_path, header, base):
    write(tmp_path, "src/b.py", f"{header}\nclass B({base}):\n    pass\n")
    assert [f.path for f in cd.scan_project(tmp_path)] == ["src.b:B"]


def test_processors_are_found_and_a_class_through_another_class_counts(tmp_path):
    write(
        tmp_path,
        "src/x.py",
        "from raccoon_cam import CamBackend, CamProcessor\n"
        "class _Hidden(CamBackend): pass\n"
        "class Base(CamBackend): pass\n"
        "class Child(Base): pass\n"
        "class Red(CamProcessor):\n    name = 'red'\n"
        "class Other: pass\n",
    )
    found = {(f.class_name, f.kind) for f in cd.scan_project(tmp_path)}
    assert found == {("Base", "backend"), ("Child", "backend"), ("Red", "processor")}


def test_without_a_src_folder_the_project_is_scanned_and_unusable_files_are_skipped(tmp_path):
    write(tmp_path, "mine.py", BACKEND)
    write(tmp_path, "broken.py", "def (:\n")
    write(tmp_path, "my-bad-name.py", BACKEND)  # not an importable module name
    write(tmp_path, ".venv/lib/x.py", BACKEND)
    write(tmp_path, "__pycache__/y.py", BACKEND)
    assert [f.path for f in cd.scan_project(tmp_path)] == ["mine:Mine"]


def test_a_class_without_a_literal_name_is_listed_by_its_class_name(tmp_path):
    write(tmp_path, "src/x.py", "from raccoon_cam import CamBackend\nclass Odd(CamBackend):\n    name = compute()\n")
    [found] = cd.scan_project(tmp_path)
    assert found.name == "Odd" and found.description == ""


def test_a_package_init_is_the_package(tmp_path):
    write(tmp_path, "src/cam/__init__.py", BACKEND)
    assert [f.module for f in cd.scan_project(tmp_path)] == ["src.cam"]


# -- the list a UI shows ---------------------------------------------------------------------------


def test_the_list_has_built_in_backends_first_then_those_of_each_project(tmp_path, monkeypatch):
    monkeypatch.setattr(cd, "builtin_backends", lambda timeout=0: [{"path": "raccoon_cam.backends.usb:UsbCameraBackend", "name": "usb", "options": []}])
    write(tmp_path / "alpha", "src/mine.py", BACKEND)
    write(tmp_path / "beta", "src/p.py", "from raccoon_cam import CamProcessor\nclass P(CamProcessor): pass\n")
    projects = [
        {"id": "u-1", "name": "Alpha", "path": str(tmp_path / "alpha")},
        {"id": "u-2", "name": "Beta", "path": str(tmp_path / "beta")},
    ]
    items = cd.list_backends(projects)
    assert [(i["name"], i["origin"]) for i in items] == [
        ("usb", {"kind": "builtin", "project": None}),
        ("mine", {"kind": "project", "project": "Alpha"}),
    ]  # a processor is not a backend, a project without one adds nothing
    project_item = items[1]
    assert project_item["path"] == "src.mine:Mine" and project_item["project_id"] == "u-1"
    assert project_item["project_dir"] == str(tmp_path / "alpha") and project_item["description"] == "My own camera."


# -- asking raccoon-cam ----------------------------------------------------------------------------


def fake_run(monkeypatch, stdout="", returncode=0, stderr="", seen=None):
    def run(command, **kwargs):
        if seen is not None:
            seen.append((command, kwargs))
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    monkeypatch.setattr(cd.subprocess, "run", run)
    monkeypatch.setattr(cd.importlib.util, "find_spec", lambda name: object())


def test_a_class_is_described_in_a_process_of_its_own_with_the_project_on_the_path(monkeypatch, tmp_path):
    seen = []
    fake_run(monkeypatch, json.dumps({"name": "mine", "options": []}), seen=seen)
    assert cd.describe_class("src.mine:Mine", tmp_path)["name"] == "mine"
    command, kwargs = seen[0]
    assert command[:3] == [sys.executable, "-m", "raccoon_cam.describe"]
    assert command[3:] == ["src.mine:Mine", "--kind", "backend", "--project-dir", str(tmp_path)]
    assert kwargs["cwd"] == str(tmp_path) and kwargs["timeout"] == cd.DESCRIBE_TIMEOUT


def test_the_error_of_the_describing_process_is_the_message(monkeypatch):
    fake_run(monkeypatch, json.dumps({"error": "ValueError: cannot load 'x:Y'"}), returncode=1)
    with pytest.raises(cd.CameraDiscoveryError, match="cannot load 'x:Y'"):
        cd.describe_class("x:Y")


def test_a_process_that_printed_nothing_useful_is_reported_with_its_last_line(monkeypatch):
    fake_run(monkeypatch, "", returncode=1, stderr="Traceback ...\nImportError: libopencv missing\n")
    with pytest.raises(cd.CameraDiscoveryError, match="ImportError: libopencv missing"):
        cd.describe_class("x:Y")


def test_a_describing_process_that_takes_too_long_is_stopped(monkeypatch):
    def run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(cd.subprocess, "run", run)
    monkeypatch.setattr(cd.importlib.util, "find_spec", lambda name: object())
    with pytest.raises(cd.CameraDiscoveryError, match="took longer than 5 s"):
        cd.describe_class("x:Y", timeout=5)


def test_without_raccoon_cam_there_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(cd.importlib.util, "find_spec", lambda name: None)
    with pytest.raises(cd.CameraUnavailable, match="not installed"):
        cd.builtin_backends()


# -- for real, with raccoon-cam in this environment ------------------------------------------------


@needs_raccoon_cam
def test_the_built_in_backends_and_their_options_come_from_raccoon_cam():
    by_name = {b["name"]: b for b in cd.builtin_backends()}
    assert set(by_name) == {"usb", "replay"}
    assert [o["name"] for o in by_name["usb"]["options"]][:3] == ["device", "resolution", "fps"]


@needs_raccoon_cam
def test_a_project_backend_is_described_by_importing_it_in_another_process(tmp_path):
    write(tmp_path, "src/__init__.py", "")
    write(tmp_path, "src/mine.py", BACKEND)
    description = cd.describe_class("src.mine:Mine", tmp_path)
    assert description["name"] == "mine" and description["options"][0]["name"] == "gain"
    assert cd.validate_config("src.mine:Mine", {"gain": "2.5"}, tmp_path) == {"gain": 2.5}
    with pytest.raises(cd.CameraDiscoveryError, match=r"unknown mine backend option\(s\): speed"):
        cd.validate_config("src.mine:Mine", {"speed": 1}, tmp_path)


# -- the chosen backend ----------------------------------------------------------------------------


@needs_raccoon_cam
def test_a_choice_is_checked_saved_read_and_cleared(tmp_path, monkeypatch):
    monkeypatch.setenv("RACCOON_CAM_STATE_DIR", str(tmp_path / "state"))
    assert cd.get_choice() is None
    saved = cd.save_choice("raccoon_cam.backends.usb:UsbCameraBackend", {"fps": "20", "resolution": [640, 480]})
    assert saved == {
        "path": "raccoon_cam.backends.usb:UsbCameraBackend",
        "cfg": {"fps": 20, "resolution": [640, 480]},
        "project_dir": None,
    }
    assert cd.get_choice() == saved
    cd.clear_choice()
    assert cd.get_choice() is None


@needs_raccoon_cam
def test_a_choice_that_does_not_fit_is_not_saved(tmp_path, monkeypatch):
    monkeypatch.setenv("RACCOON_CAM_STATE_DIR", str(tmp_path / "state"))
    with pytest.raises(cd.CameraDiscoveryError, match="fps must be at least 1"):
        cd.save_choice("raccoon_cam.backends.usb:UsbCameraBackend", {"fps": 0})
    assert cd.get_choice() is None


def test_the_state_directory_defaults_to_the_one_the_unit_creates(monkeypatch):
    monkeypatch.delenv("RACCOON_CAM_STATE_DIR", raising=False)
    monkeypatch.delenv("STATE_DIRECTORY", raising=False)
    assert cd.state_file() == Path("/var/lib/raccoon-cam/backend.json")
