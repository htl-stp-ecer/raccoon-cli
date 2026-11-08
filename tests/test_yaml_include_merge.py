"""!include-merge must reject keys defined more than once."""

import pytest

from raccoon_cli.yaml_utils import load_yaml


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_merge_without_collision(tmp_path):
    _write(tmp_path / "config/servos.yml", "arm: {type: Servo, port: 3}\n")
    _write(
        tmp_path / "p.yml",
        "definitions:\n  button: {type: DigitalSensor, port: 1}\n"
        "  _s: !include-merge config/servos.yml\n",
    )
    defs = load_yaml(tmp_path / "p.yml")["definitions"]
    assert defs["arm"]["port"] == 3
    assert defs["button"]["port"] == 1


def test_merge_collides_with_inline_key(tmp_path):
    _write(tmp_path / "config/servos.yml", "arm: {type: Servo, port: 3}\n")
    _write(
        tmp_path / "p.yml",
        "definitions:\n  arm: {type: Servo, port: 0}\n"
        "  _s: !include-merge config/servos.yml\n",
    )
    with pytest.raises(ValueError, match="Duplicate key 'arm'.*inline.*servos.yml"):
        load_yaml(tmp_path / "p.yml")


def test_two_merges_collide(tmp_path):
    _write(tmp_path / "config/a.yml", "arm: {type: Servo, port: 3}\n")
    _write(tmp_path / "config/b.yml", "arm: {type: Servo, port: 0}\n")
    _write(
        tmp_path / "p.yml",
        "definitions:\n  _a: !include-merge config/a.yml\n"
        "  _b: !include-merge config/b.yml\n",
    )
    with pytest.raises(ValueError, match="Duplicate key 'arm'.*a.yml.*b.yml"):
        load_yaml(tmp_path / "p.yml")
