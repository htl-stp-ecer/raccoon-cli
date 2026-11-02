from __future__ import annotations

import pytest
from click.testing import CliRunner

from raccoon_cli import mission_ops
from raccoon_cli.cli import main
from raccoon_cli.mission_ops import MissionError
from raccoon_cli.naming import mission_module_name
from raccoon_cli.project import load_project_config

MISSIONS = [
    {"M000SetupMission": "setup"},
    "M010DriveMission",
    "M020CollectMission",
    {"M999ShutdownMission": "shutdown"},
]


def _write_mission(root, class_name: str, extra: str = "") -> None:
    f = root / "src" / "missions" / f"{mission_module_name(class_name)}.py"
    f.write_text(f"class {class_name}(Mission):\n    pass\n{extra}", encoding="utf-8")


@pytest.fixture
def project(tmp_path, monkeypatch):
    import yaml

    (tmp_path / "src" / "missions").mkdir(parents=True)
    (tmp_path / "raccoon.project.yml").write_text(
        yaml.safe_dump({"name": "Bot", "uuid": "u-1", "missions": MISSIONS}), encoding="utf-8"
    )
    for entry in MISSIONS:
        _write_mission(tmp_path, mission_ops.mission_entry_name(entry))
    (tmp_path / "src" / "main.py").write_text(
        "from src.missions.m010_drive_mission import M010DriveMission\n"
        "from src.missions.m020_collect_mission import M020CollectMission\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _names(root) -> list[str]:
    return [mission_ops.mission_entry_name(e) for e in load_project_config(root)["missions"]]


def _files(root) -> set[str]:
    return {p.name for p in (root / "src" / "missions").glob("*.py")}


def test_module_name_matches_generator_convention():
    assert mission_module_name("M010DriveFooMission") == "m010_drive_foo_mission"


def test_list_reports_kind_and_missing_files(project):
    (project / "src" / "missions" / "m020_collect_mission.py").unlink()
    infos = mission_ops.list_missions(project)

    assert [i.class_name for i in infos] == _names(project)
    assert [i.kind for i in infos] == ["setup", None, None, "shutdown"]
    assert [i.file_exists for i in infos] == [True, True, False, True]


@pytest.mark.parametrize("query", ["Drive", "DriveMission", "M010Drive", "m010_drive", "M010DriveMission"])
def test_resolve_accepts_name_variants(query):
    assert mission_ops.resolve_mission(MISSIONS, query) == 1


def test_resolve_is_exact_unless_prefix_allowed():
    with pytest.raises(MissionError):
        mission_ops.resolve_mission(MISSIONS, "Dri")
    assert mission_ops.resolve_mission(MISSIONS, "Dri", allow_prefix=True) == 1


def test_resolve_rejects_unknown_and_ambiguous():
    with pytest.raises(MissionError, match="No mission"):
        mission_ops.resolve_mission(MISSIONS, "Nope")
    with pytest.raises(MissionError, match="ambiguous"):
        mission_ops.resolve_mission(["M010FooMission", "M020FooMission"], "Foo")


def test_remove_deletes_config_entry_and_file(project):
    result = mission_ops.remove_mission(project, "Drive")

    assert result.class_name == "M010DriveMission" and result.file_deleted
    assert "M010DriveMission" not in _names(project)
    assert "m010_drive_mission.py" not in _files(project)


def test_remove_keep_file(project):
    mission_ops.remove_mission(project, "Drive", keep_file=True)

    assert "M010DriveMission" not in _names(project)
    assert "m010_drive_mission.py" in _files(project)


def test_rename_keeps_number_and_patches_file_and_references(project):
    result = mission_ops.rename_mission(project, "Drive", "Sprint To Wall")

    assert (result.old_class, result.new_class) == ("M010DriveMission", "M010SprintToWallMission")
    assert _names(project)[1] == "M010SprintToWallMission"
    assert "m010_sprint_to_wall_mission.py" in _files(project)
    assert "m010_drive_mission.py" not in _files(project)
    body = (project / "src" / "missions" / "m010_sprint_to_wall_mission.py").read_text()
    assert "class M010SprintToWallMission(Mission)" in body
    main_py = (project / "src" / "main.py").read_text()
    assert "src.missions.m010_sprint_to_wall_mission import M010SprintToWallMission" in main_py
    assert "M020CollectMission" in main_py  # untouched


def test_rename_preserves_setup_role(project):
    mission_ops.rename_mission(project, "Setup", "Init")
    assert load_project_config(project)["missions"][0] == {"M000InitMission": "setup"}


def test_rename_rejects_collisions_and_noop(project):
    (project / "src" / "missions" / "m010_collect_mission.py").write_text("# stray file\n")
    with pytest.raises(MissionError, match="already exists"):
        mission_ops.rename_mission(project, "Drive", "Collect")
    assert "M010DriveMission" in _names(project)  # nothing was changed
    with pytest.raises(MissionError, match="already named"):
        mission_ops.rename_mission(project, "Drive", "M010DriveMission")


def test_reorder_by_index_swaps_and_renumbers(project):
    cfg = load_project_config(project)["missions"]
    ordered = mission_ops.resolve_order(cfg, ["1", "3", "2", "4"])
    moves = mission_ops.reorder_missions(project, ordered)

    assert _names(project) == ["M000SetupMission", "M010CollectMission", "M020DriveMission", "M999ShutdownMission"]
    assert {"m010_collect_mission.py", "m020_drive_mission.py"} <= _files(project)
    assert len(moves) == 2
    assert "class M010CollectMission" in (project / "src" / "missions" / "m010_collect_mission.py").read_text()
    main_py = (project / "src" / "main.py").read_text()
    # swapped numbers must not be applied twice
    assert "m020_drive_mission import M020DriveMission" in main_py
    assert "m010_collect_mission import M010CollectMission" in main_py
    assert load_project_config(project)["missions"][0] == {"M000SetupMission": "setup"}


def test_reorder_by_names_and_errors(project):
    cfg = load_project_config(project)["missions"]
    ordered = mission_ops.resolve_order(cfg, ["Setup", "Collect", "Drive", "Shutdown"])
    assert [mission_ops.mission_entry_name(e) for e in ordered][1] == "M020CollectMission"

    with pytest.raises(MissionError, match="permutation"):
        mission_ops.resolve_order(cfg, ["1", "2"])
    with pytest.raises(MissionError, match="Missing"):
        mission_ops.resolve_order(cfg, ["Setup", "Drive"])
    with pytest.raises(MissionError, match="more than once"):
        mission_ops.resolve_order(cfg, ["Setup", "Drive", "Drive", "Shutdown"])


def _run(*args):
    return CliRunner().invoke(main, ["--no-validate", "missions", *args], catch_exceptions=False)


def test_cli_group_and_old_commands(project):
    result = CliRunner().invoke(main, ["missions", "--help"])
    for sub in ("create", "remove", "list", "reorder", "rename"):
        assert sub in result.output
    for old in ("create", "remove", "list", "reorder"):
        assert CliRunner().invoke(main, [old, "--help"]).exit_code != 0


def test_cli_create_list_rename_remove_roundtrip(project):
    assert _run("create", "Wave").exit_code == 0
    assert "M030WaveMission" in _names(project)

    listing = _run("list")
    assert listing.exit_code == 0 and "M030WaveMission" in listing.output

    assert _run("rename", "Wave", "Salute").exit_code == 0
    assert "M030SaluteMission" in _names(project)

    assert _run("remove", "Salute").exit_code == 0
    assert "M030SaluteMission" not in _names(project)


def test_cli_reports_errors_without_traceback(project):
    result = CliRunner().invoke(main, ["--no-validate", "missions", "remove", "Nope"])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
