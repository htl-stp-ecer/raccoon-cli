from types import SimpleNamespace

import pytest

from raccoon_cli.ide.sim.runner import _resolve_scene


def test_scene_search_prefers_project_then_configured_directory_then_wheel(tmp_path, monkeypatch):
    project = tmp_path / "project"
    configured = tmp_path / "configured"
    package = tmp_path / "raccoon"
    scenes = package / "scenes"
    project.mkdir()
    configured.mkdir()
    scenes.mkdir(parents=True)
    monkeypatch.setenv("RACCOON_SIM_SCENE_DIR", str(configured))
    monkeypatch.setattr(
        "raccoon_cli.ide.sim.runner.importlib.util.find_spec",
        lambda name: SimpleNamespace(submodule_search_locations=[str(package)]),
    )
    name = "empty_table.ftmap"
    bundled = scenes / name
    bundled.touch()
    assert _resolve_scene(name, project) == bundled

    shared = configured / name
    shared.touch()
    assert _resolve_scene(name, project) == shared

    local = project / name
    local.touch()
    assert _resolve_scene(name, project) == local
    assert _resolve_scene(str(bundled), project) == bundled

    with pytest.raises(SystemExit, match="Scene not found.*custom"):
        _resolve_scene("custom/empty_table.ftmap", project)
