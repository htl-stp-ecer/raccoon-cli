from __future__ import annotations

from io import StringIO

from rich.console import Console

from raccoon_cli import laptop_update
from raccoon_cli.version_checker import (
    PACKAGE_REGISTRY,
    PackageStatus,
    render_version_table,
)

PYPROJECT = """\
[project]
name = "my-robot"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = [
    "raccoon-cli>=1.0",
    'raccoon_library[sim]~=2.0',
    "httpx",
]

[build-system]
requires = ["hatchling"]
"""


def test_apply_pins_rewrites_versions_and_project_version(tmp_path):
    f = tmp_path / "pyproject.toml"
    f.write_text(PYPROJECT)

    changed, missing = laptop_update.apply_pins(
        f, {"raccoon-cli": "1.5.0", "raccoon-library": "2.3.1"}, "2026.4.25.1"
    )

    text = f.read_text()
    assert changed == ["raccoon-cli", "raccoon-library"] and missing == []
    assert '"raccoon-cli==1.5.0"' in text
    assert "'raccoon_library[sim]==2.3.1'" in text
    assert 'version = "2026.4.25.1"' in text
    assert '"httpx"' in text and 'requires = ["hatchling"]' in text


def test_apply_pins_ignores_non_pep440_bundle_and_reports_missing(tmp_path):
    f = tmp_path / "pyproject.toml"
    f.write_text(PYPROJECT)

    changed, missing = laptop_update.apply_pins(f, {"raccoon-cli": "1.5.0", "other": "1"}, "dev")

    assert changed == ["raccoon-cli"] and missing == ["other"]
    assert 'version = "0.1.0"' in f.read_text()


def test_stubs_removed_from_registry():
    assert "raccoon-stubs" not in {p.name for p in PACKAGE_REGISTRY}


def _render(statuses, **kw) -> str:
    buf = StringIO()
    render_version_table(Console(file=buf, width=100, color_system=None), statuses, **kw)
    return buf.getvalue()


def test_tables_split_and_empty_bundle_version_shows_dash():
    by_name = {p.name: p for p in PACKAGE_REGISTRY}
    statuses = [
        PackageStatus(by_name["raccoon-cli"], "1.0", "1.0", "0.9"),
        PackageStatus(by_name["raccoon-cam"], None, None, None),
    ]

    out = _render(statuses)
    assert out.startswith("\n")
    assert "Laptop Versions" in out and "Pi Versions" in out
    assert out.index("Laptop Versions") < out.index("Pi Versions")
    cam_row = next(l for l in out.splitlines() if "raccoon-cam" in l)
    assert " - " in cam_row and "?" not in out

    laptop_only = _render(statuses, show_pi=False)
    assert "Pi Versions" not in laptop_only and "raccoon-cam" not in laptop_only


def test_missing_manifest_component_is_dash_and_never_hits_github(monkeypatch):
    from raccoon_cli import version_checker as vc

    def boom(repo):
        raise AssertionError("GitHub must not be queried when a manifest exists")

    monkeypatch.setattr(vc, "get_latest_version", boom)
    monkeypatch.setattr(vc, "get_local_pip_version", lambda name: None)

    statuses = vc.check_all_versions(manifest={"bundle": "dev", "components": {"raccoon-cli": "v5"}})
    by_name = {s.info.name: s for s in statuses}

    assert by_name["raccoon-cli"].latest_version == "5"
    assert by_name["raccoon-transport"].latest_version is None
