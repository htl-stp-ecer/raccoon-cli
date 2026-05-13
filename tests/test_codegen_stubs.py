"""Tests for raccoon-cli codegen introspection and stub handling.

Two categories:
  - Unit tests (no raccoon needed): test the .pyi parsing logic in isolation
    using synthetic stub files written to tmp_path. Always run.
  - Integration tests (raccoon needed): test against the actually installed
    raccoon package. Skipped if raccoon is not importable.

The integration tests guard against the exact regression where:
  1. hal.pyi was not installed → DigitalSensor could not be resolved
  2. Motor.__init__ params were unavailable → codegen emitted dict literals
     instead of MotorCalibration(...) constructor calls
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest

raccoon_installed = importlib.util.find_spec("raccoon") is not None


def _raccoon_runtime_available() -> bool:
    """True when the real raccoon runtime (not just stubs) is installed."""
    try:
        import raccoon
        return hasattr(raccoon, "Motor")
    except ImportError:
        return False


raccoon_runtime_available = _raccoon_runtime_available()
requires_raccoon = pytest.mark.skipif(
    not raccoon_runtime_available,
    reason="raccoon runtime not installed (only stubs present)",
)


@pytest.fixture(scope="session", autouse=True)
def ensure_latest_raccoon_stubs():
    """Always pull the latest raccoon-stubs from PyPI before integration tests run."""
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", "raccoon-stubs", "-q"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# ---------------------------------------------------------------------------
# Unit tests — pyi parsing (no raccoon required)
# ---------------------------------------------------------------------------

class TestFindPyiForModule:
    """_find_pyi_for_module locates sibling .pyi files correctly."""

    def test_returns_none_for_unknown_module(self):
        from raccoon_cli.codegen.introspection import _find_pyi_for_module
        assert _find_pyi_for_module("this_module_does_not_exist") is None

    def test_returns_none_when_pyi_absent(self, tmp_path, monkeypatch):
        """When the .so exists but no .pyi sibling, returns None."""
        import importlib.util as ilu
        so = tmp_path / "mymod.cpython-313-x86_64-linux-gnu.so"
        so.touch()

        fake_spec = type("FakeSpec", (), {"origin": str(so)})()
        monkeypatch.setattr(ilu, "find_spec", lambda name: fake_spec if name == "mymod" else None)

        from raccoon_cli.codegen.introspection import _find_pyi_for_module
        assert _find_pyi_for_module("mymod") is None

    def test_returns_path_when_pyi_present(self, tmp_path, monkeypatch):
        import importlib.util as ilu
        so = tmp_path / "mymod.cpython-313-x86_64-linux-gnu.so"
        so.touch()
        pyi = tmp_path / "mymod.pyi"
        pyi.write_text("class Foo:\n    def __init__(self, x: int) -> None: ...\n")

        fake_spec = type("FakeSpec", (), {"origin": str(so)})()
        monkeypatch.setattr(ilu, "find_spec", lambda name: fake_spec if name == "mymod" else None)

        from raccoon_cli.codegen.introspection import _find_pyi_for_module
        result = _find_pyi_for_module("mymod")
        assert result == pyi


class TestParseInitFromPyi:
    """_parse_init_from_pyi correctly extracts __init__ parameters."""

    def _write_pyi(self, tmp_path: Path, content: str) -> Path:
        pyi = tmp_path / "fake.pyi"
        pyi.write_text(dedent(content))
        return pyi

    def _make_cls_proxy(self, module_name: str, class_name: str, pyi: Path, monkeypatch):
        """Return a fake class whose module resolves to *pyi*."""
        import importlib.util as ilu
        fake_spec = type("FakeSpec", (), {"origin": str(pyi.parent / f"{pyi.stem}.so")})()
        monkeypatch.setattr(ilu, "find_spec", lambda n: fake_spec if n == module_name else None)

        cls = type(class_name, (), {"__module__": module_name, "__name__": class_name})
        return cls

    def test_required_param(self, tmp_path, monkeypatch):
        pyi = self._write_pyi(tmp_path, """\
            class Widget:
                def __init__(self, port: int) -> None: ...
        """)
        cls = self._make_cls_proxy("mymod", "Widget", pyi, monkeypatch)

        from raccoon_cli.codegen.introspection import _parse_init_from_pyi
        params = _parse_init_from_pyi(cls)
        assert params is not None
        assert "port" in params
        assert params["port"].default is inspect.Parameter.empty

    def test_optional_param(self, tmp_path, monkeypatch):
        pyi = self._write_pyi(tmp_path, """\
            class Widget:
                def __init__(self, port: int, inverted: bool = False) -> None: ...
        """)
        cls = self._make_cls_proxy("mymod", "Widget", pyi, monkeypatch)

        from raccoon_cli.codegen.introspection import _parse_init_from_pyi
        params = _parse_init_from_pyi(cls)
        assert "inverted" in params
        assert params["inverted"].default is not inspect.Parameter.empty

    def test_self_excluded(self, tmp_path, monkeypatch):
        pyi = self._write_pyi(tmp_path, """\
            class Widget:
                def __init__(self, port: int) -> None: ...
        """)
        cls = self._make_cls_proxy("mymod", "Widget", pyi, monkeypatch)

        from raccoon_cli.codegen.introspection import _parse_init_from_pyi
        params = _parse_init_from_pyi(cls)
        assert "self" not in params

    def test_overload_picks_most_params(self, tmp_path, monkeypatch):
        """When multiple @overload __init__ exist, pick the one with the most params."""
        pyi = self._write_pyi(tmp_path, """\
            class Widget:
                def __init__(self, port: int) -> None: ...
                def __init__(self, port: int, inverted: bool = False) -> None: ...
        """)
        cls = self._make_cls_proxy("mymod", "Widget", pyi, monkeypatch)

        from raccoon_cli.codegen.introspection import _parse_init_from_pyi
        params = _parse_init_from_pyi(cls)
        assert "inverted" in params

    def test_returns_none_for_unknown_class(self, tmp_path, monkeypatch):
        pyi = self._write_pyi(tmp_path, """\
            class Other:
                def __init__(self) -> None: ...
        """)
        cls = self._make_cls_proxy("mymod", "Widget", pyi, monkeypatch)

        from raccoon_cli.codegen.introspection import _parse_init_from_pyi
        assert _parse_init_from_pyi(cls) is None


class TestParseParamTypeFromPyi:
    """_parse_param_type_from_pyi resolves type annotations from stubs."""

    def test_resolves_builtin_type_annotation(self, tmp_path, monkeypatch):
        import importlib.util as ilu
        pyi = tmp_path / "mymod.pyi"
        pyi.write_text("class Widget:\n    def __init__(self, port: int) -> None: ...\n")
        fake_spec = type("FakeSpec", (), {"origin": str(tmp_path / "mymod.so")})()
        monkeypatch.setattr(ilu, "find_spec", lambda n: fake_spec if n == "mymod" else None)

        cls = type("Widget", (), {"__module__": "mymod", "__name__": "Widget"})
        from raccoon_cli.codegen.introspection import _parse_param_type_from_pyi
        # int is a builtin — resolve_class won't find it, returns None; that's fine
        # This test just verifies no exception is raised
        _parse_param_type_from_pyi(cls, "port")  # should not raise


# ---------------------------------------------------------------------------
# Stub-fallback tests — require raccoon-stubs but NOT the runtime
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not raccoon_installed, reason="raccoon-stubs not installed")
class TestStubFallback:
    """Verify stub-based class resolution works with only raccoon-stubs installed."""

    def test_find_pyi_for_namespace_package(self, tmp_path, monkeypatch):
        import importlib.util as ilu
        from raccoon_cli.codegen.introspection import _find_pyi_for_module

        fake_spec = type("FakeSpec", (), {
            "origin": None,
            "submodule_search_locations": [str(tmp_path)],
        })()
        pyi = tmp_path / "__init__.pyi"
        pyi.write_text("class Foo: ...")
        monkeypatch.setattr(ilu, "find_spec", lambda n: fake_spec if n == "mypkg" else None)

        result = _find_pyi_for_module("mypkg")
        assert result == pyi

    def test_resolve_class_falls_back_to_pyi(self):
        from raccoon_cli.codegen.introspection import _stub_class_registry, resolve_class
        _stub_class_registry.clear()
        cls = resolve_class("raccoon.IMU")
        assert cls.__name__ == "IMU"

    def test_analog_sensor_issubclass_preserved(self):
        from raccoon_cli.codegen.introspection import _stub_class_registry, resolve_class
        _stub_class_registry.clear()
        a = resolve_class("raccoon.AnalogSensor")
        b = resolve_class("raccoon.AnalogSensor")
        assert issubclass(a, b)

    def test_ir_sensor_issubclass_analog_sensor(self):
        from raccoon_cli.codegen.introspection import _stub_class_registry, resolve_class
        _stub_class_registry.clear()
        ir = resolve_class("raccoon.IRSensor")
        analog = resolve_class("raccoon.AnalogSensor")
        assert issubclass(ir, analog)

    def test_defs_generator_init_does_not_crash(self):
        from raccoon_cli.codegen.introspection import _stub_class_registry
        _stub_class_registry.clear()
        from raccoon_cli.codegen.generators.defs_generator import DefsGenerator
        gen = DefsGenerator()
        assert gen._imu_class.__name__ == "IMU"
        assert gen._analog_sensor_class.__name__ == "AnalogSensor"


# ---------------------------------------------------------------------------
# Integration tests — require raccoon installed
# ---------------------------------------------------------------------------

@requires_raccoon
class TestResolveClass:
    """resolve_class() correctly imports types from raccoon's native modules."""

    def test_resolves_digital_sensor(self):
        from raccoon_cli.codegen.introspection import resolve_class
        cls = resolve_class("raccoon.hal.DigitalSensor")
        assert cls.__name__ == "DigitalSensor"

    def test_resolves_analog_sensor(self):
        from raccoon_cli.codegen.introspection import resolve_class
        cls = resolve_class("raccoon.hal.AnalogSensor")
        assert cls.__name__ == "AnalogSensor"

    def test_resolves_motor(self):
        from raccoon_cli.codegen.introspection import resolve_class
        cls = resolve_class("raccoon.hal.Motor")
        assert cls.__name__ == "Motor"

    def test_resolves_servo(self):
        from raccoon_cli.codegen.introspection import resolve_class
        cls = resolve_class("raccoon.hal.Servo")
        assert cls.__name__ == "Servo"

    def test_resolves_motor_calibration(self):
        from raccoon_cli.codegen.introspection import resolve_class
        cls = resolve_class("raccoon.foundation.MotorCalibration")
        assert cls.__name__ == "MotorCalibration"

    def test_raises_for_nonexistent_class(self):
        from raccoon_cli.codegen.introspection import resolve_class
        with pytest.raises(ImportError):
            resolve_class("raccoon.hal.ThisDoesNotExist")


@requires_raccoon
class TestGetInitParams:
    """get_init_params() falls back to .pyi stubs for pybind11 native classes.

    If hal.pyi is not installed the params dict will be empty and these tests
    fail — which is the regression we want to catch.
    """

    def test_motor_port_is_required(self):
        from raccoon.hal import Motor
        from raccoon_cli.codegen.introspection import get_init_params
        params = get_init_params(Motor)
        assert "port" in params, (
            f"Motor.port missing — hal.pyi may not be installed. Got: {list(params)}"
        )
        assert params["port"].default is inspect.Parameter.empty, "Motor.port must be required"

    def test_motor_inverted_is_optional(self):
        from raccoon.hal import Motor
        from raccoon_cli.codegen.introspection import get_init_params
        params = get_init_params(Motor)
        assert "inverted" in params, f"Motor.inverted missing. Got: {list(params)}"
        assert params["inverted"].default is not inspect.Parameter.empty

    def test_motor_calibration_is_optional(self):
        from raccoon.hal import Motor
        from raccoon_cli.codegen.introspection import get_init_params
        params = get_init_params(Motor)
        assert "calibration" in params, f"Motor.calibration missing. Got: {list(params)}"
        assert params["calibration"].default is not inspect.Parameter.empty

    def test_servo_port_is_required(self):
        from raccoon.hal import Servo
        from raccoon_cli.codegen.introspection import get_init_params
        params = get_init_params(Servo)
        assert "port" in params, f"Servo.port missing. Got: {list(params)}"
        assert params["port"].default is inspect.Parameter.empty

    def test_digital_sensor_port_is_required(self):
        from raccoon.hal import DigitalSensor
        from raccoon_cli.codegen.introspection import get_init_params
        params = get_init_params(DigitalSensor)
        assert "port" in params, f"DigitalSensor.port missing. Got: {list(params)}"
        assert params["port"].default is inspect.Parameter.empty

    def test_analog_sensor_port_is_required(self):
        from raccoon.hal import AnalogSensor
        from raccoon_cli.codegen.introspection import get_init_params
        params = get_init_params(AnalogSensor)
        assert "port" in params, f"AnalogSensor.port missing. Got: {list(params)}"
        assert params["port"].default is inspect.Parameter.empty


@requires_raccoon
class TestInferParamType:
    """infer_param_type() reads type annotations from .pyi stubs."""

    def test_motor_calibration_resolves_to_motor_calibration_class(self):
        from raccoon.hal import Motor
        from raccoon_cli.codegen.introspection import infer_param_type
        t = infer_param_type(Motor, "calibration")
        assert t is not None, (
            "Could not infer type for Motor.calibration — hal.pyi may not expose the annotation"
        )
        assert t.__name__ == "MotorCalibration"


@requires_raccoon
class TestCodegenEndToEnd:
    """Full codegen pipeline produces correct Python for common hardware configs."""

    def _run_defs_gen(self, config: dict) -> str:
        from raccoon_cli.codegen.generators.defs_generator import DefsGenerator
        gen = DefsGenerator()
        data = gen.extract_config(config)
        gen.validate_config(data)
        return gen.generate_body(data)

    def test_digital_sensor_generates_constructor(self):
        """DigitalSensor(port=10) must appear in the generated body — not a dict literal."""
        body = self._run_defs_gen({
            "definitions": {
                "button": {"type": "DigitalSensor", "port": 10},
            }
        })
        assert "DigitalSensor(port=10)" in body, (
            f"Expected DigitalSensor(port=10) in generated body.\nActual body:\n{body}"
        )

    def test_motor_calibration_generates_constructor_not_dict(self):
        """Motor with a calibration dict must produce MotorCalibration(...) in the output.

        Regression: without hal.pyi and foundation.pyi installed, the codegen
        could not infer the calibration param type and emitted a raw dict literal.
        """
        body = self._run_defs_gen({
            "definitions": {
                "button": {"type": "DigitalSensor", "port": 10},
                "drive_motor": {
                    "type": "Motor",
                    "port": 1,
                    "inverted": False,
                    "calibration": {"ticks_to_rad": 0.001, "vel_lpf_alpha": 0.5},
                },
            }
        })
        assert "MotorCalibration(" in body, (
            "Expected MotorCalibration(...) constructor in generated body.\n"
            "Got a raw dict — foundation.pyi or hal.pyi type annotation may be missing.\n"
            f"Actual body:\n{body}"
        )
        assert '{"ticks_to_rad"' not in body, (
            "Motor.calibration was emitted as a dict literal instead of MotorCalibration(...)"
        )

    def test_servo_generates_constructor(self):
        body = self._run_defs_gen({
            "definitions": {
                "button": {"type": "DigitalSensor", "port": 10},
                "arm": {"type": "Servo", "port": 0},
            }
        })
        assert "Servo(port=0)" in body

    def test_no_codegen_errors_for_full_config(self):
        """A realistic multi-component config must generate without raising."""
        config = {
            "definitions": {
                "button": {"type": "DigitalSensor", "port": 10},
                "line_left": {"type": "AnalogSensor", "port": 0},
                "line_right": {"type": "AnalogSensor", "port": 1},
                "motor_l": {
                    "type": "Motor",
                    "port": 0,
                    "inverted": True,
                    "calibration": {"ticks_to_rad": 0.0004, "vel_lpf_alpha": 0.8},
                },
                "motor_r": {
                    "type": "Motor",
                    "port": 1,
                    "inverted": False,
                    "calibration": {"ticks_to_rad": 0.0004, "vel_lpf_alpha": 0.8},
                },
                "gripper": {"type": "Servo", "port": 0},
            }
        }
        body = self._run_defs_gen(config)
        assert "class Defs" in body
