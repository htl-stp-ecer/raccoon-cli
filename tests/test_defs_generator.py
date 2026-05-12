"""Regression tests for defs generator edge cases."""

from raccoon_cli.codegen.generators.defs_generator import DefsGenerator


class _AlwaysFallbackResolver:
    """Resolver stub that forces unresolved-constructor fallback paths."""

    def resolve_from_config(self, _cfg, type_key="type"):
        raise ValueError("forced fallback")


def test_servo_positions_preserved_in_fallback_codegen():
    """Servo presets should still be generated when fallback constructors are used."""
    gen = DefsGenerator()
    gen.resolver = _AlwaysFallbackResolver()  # type: ignore[assignment]

    config = {
        "definitions": {
            "button": {"type": "DigitalSensor", "port": 10},
            "arm_servo": {
                "type": "Servo",
                "port": 0,
                "positions": {"up": 75, "down": 165},
            },
        }
    }

    source = gen.generate(config)

    assert "from raccoon.step.servo.preset import ServoPreset" in source
    assert (
        'arm_servo = ServoPreset(Servo(port=0), positions={"up": 75, "down": 165})'
        in source
    )
