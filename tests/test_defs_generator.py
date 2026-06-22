"""Regression tests for defs generator edge cases."""

from textwrap import dedent

import pytest
from raccoon_cli.codegen.builder import ImportSet, build_constructor_expr
from raccoon_cli.codegen.generators.defs_generator import DefsGenerator


class _AlwaysFailResolver:
    """Resolver stub that always raises — used to verify errors propagate."""

    def resolve_from_config(self, _cfg, type_key="type"):
        raise ValueError("unknown type")


def _stale_motor_calibration_cls(tmp_path, monkeypatch):
    """A MotorCalibration proxy whose installed .pyi predates bemf_offset.

    Mirrors the real-world state where the locally-installed raccoon is an
    older PyPI build: the stub's __init__ overloads only know ticks_to_rad
    and vel_lpf_alpha, so introspection never sees bemf_offset.
    """
    import importlib.util as ilu

    pyi = tmp_path / "foundation.pyi"
    pyi.write_text(dedent("""\
        class MotorCalibration:
            def __init__(self) -> None: ...
            def __init__(self, ticks_to_rad: float, vel_lpf_alpha: float) -> None: ...
    """))
    fake_spec = type("FakeSpec", (), {"origin": str(tmp_path / "foundation.so")})()
    monkeypatch.setattr(
        ilu, "find_spec", lambda n: fake_spec if n == "raccoon.foundation" else None
    )
    return type(
        "MotorCalibration",
        (),
        {"__module__": "raccoon.foundation", "__name__": "MotorCalibration"},
    )


def test_bemf_offset_flows_through_with_stale_stub(tmp_path, monkeypatch):
    """A bemf_offset key in the YAML calibration dict must reach the emitted
    MotorCalibration(...) call even when the installed stub does not know the
    parameter — codegen builds kwargs from the YAML, not from introspection."""
    cls = _stale_motor_calibration_cls(tmp_path, monkeypatch)

    expr = build_constructor_expr(
        cls,
        {"ticks_to_rad": 1.77e-05, "vel_lpf_alpha": 1.0, "bemf_offset": 3.5},
        "definitions.front_left_motor.calibration",
        ImportSet(),
    )

    assert "bemf_offset=3.5" in expr
    assert expr.startswith("MotorCalibration(")


def test_bemf_offset_omitted_when_absent(tmp_path, monkeypatch):
    """When the YAML calibration dict has no bemf_offset, none is emitted."""
    cls = _stale_motor_calibration_cls(tmp_path, monkeypatch)

    expr = build_constructor_expr(
        cls,
        {"ticks_to_rad": 1.77e-05, "vel_lpf_alpha": 1.0},
        "definitions.front_left_motor.calibration",
        ImportSet(),
    )

    assert "bemf_offset" not in expr


def test_unknown_type_raises_value_error():
    """Unresolvable types must raise ValueError, not silently fall back."""
    gen = DefsGenerator()
    gen.resolver = _AlwaysFailResolver()  # type: ignore[assignment]

    config = {
        "definitions": {
            "button": {"type": "DigitalSensor", "port": 10},
        }
    }

    with pytest.raises(ValueError, match="definitions.button"):
        gen.generate(config)
