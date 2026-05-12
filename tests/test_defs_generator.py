"""Regression tests for defs generator edge cases."""

import pytest
from raccoon_cli.codegen.generators.defs_generator import DefsGenerator


class _AlwaysFailResolver:
    """Resolver stub that always raises — used to verify errors propagate."""

    def resolve_from_config(self, _cfg, type_key="type"):
        raise ValueError("unknown type")


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
