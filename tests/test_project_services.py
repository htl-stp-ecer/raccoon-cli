from pathlib import Path

import pytest

from raccoon_cli.project_services import (
    deploy_project_services,
    load_project_services,
    render_systemd_unit,
)


def test_load_project_services_requires_module_or_command(tmp_path: Path):
    config = {
        "uuid": "abc",
        "services": {
            "vision": {
                "module": "src.daemons.vision",
                "command": "python3 other.py",
            }
        },
    }

    with pytest.raises(ValueError, match="exactly one"):
        load_project_services(config, tmp_path)


def test_render_systemd_unit_uses_project_scoped_name_and_workdir(tmp_path: Path):
    service = load_project_services(
        {
            "uuid": "project-123",
            "services": {
                "vision": {
                    "module": "src.daemons.vision",
                    "env": {"CAMERA_DEVICE": "/dev/video0"},
                }
            },
        },
        tmp_path,
    )[0]

    unit = render_systemd_unit(service)

    assert "Description=Raccoon project service vision (project-123)" in unit
    assert f"WorkingDirectory={tmp_path}" in unit
    assert "ExecStart=/usr/bin/python3 -m src.daemons.vision" in unit
    assert 'Environment="CAMERA_DEVICE=/dev/video0"' in unit
    assert service.systemd_name == "raccoon-project-project-123-vision.service"


def test_deploy_project_services_restarts_only_when_changed(tmp_path: Path):
    (tmp_path / "raccoon.project.yml").write_text("uuid: project-123\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "daemon.py").write_text("print('daemon')\n", encoding="utf-8")

    config = {
        "uuid": "project-123",
        "services": {
            "vision": {
                "module": "src.daemon",
                "watch": ["src/daemon.py"],
                "after_sync": "restart_if_changed",
            }
        },
    }

    calls: list[list[str]] = []

    def runner(cmd, check):
        calls.append(cmd)

    results = deploy_project_services(config, tmp_path, runner=runner)

    assert [r.systemd_name for r in results] == ["raccoon-project-project-123-vision.service"]
    assert results[0].action == "restart"
    assert results[0].first_deploy is True
    assert results[0].digest_changed is True
    assert ["sudo", "systemctl", "restart", "raccoon-project-project-123-vision.service"] in calls

    calls.clear()
    results = deploy_project_services(config, tmp_path, runner=runner)

    assert results[0].action == "start"
    assert results[0].first_deploy is False
    assert results[0].digest_changed is False
    assert "unchanged" in results[0].reason
    assert ["sudo", "systemctl", "start", "raccoon-project-project-123-vision.service"] in calls
    assert ["sudo", "systemctl", "restart", "raccoon-project-project-123-vision.service"] not in calls

    (tmp_path / "src" / "daemon.py").write_text("print('changed')\n", encoding="utf-8")
    calls.clear()
    results = deploy_project_services(config, tmp_path, runner=runner)

    assert results[0].action == "restart"
    assert results[0].first_deploy is False
    assert results[0].digest_changed is True
    assert "watched files changed" in results[0].reason
    assert ["sudo", "systemctl", "restart", "raccoon-project-project-123-vision.service"] in calls
