"""Tests for the shared table-map (``.ftmap``) v2 schema and validation.

Covers the hard v1 drop: only ``version == 2`` documents are accepted, and
legacy v1 (flat ``lines[]``) payloads are rejected everywhere.
"""

import pytest
from pydantic import ValidationError

from raccoon_cli.table_map import (
    TABLE_MAP_FORMAT,
    TABLE_MAP_VERSION,
    TableMapRequest,
    TableMapVersionError,
    parse_v2,
)


def _line(kind="line"):
    return {
        "kind": kind,
        "startX": 0.0,
        "startY": 0.0,
        "endX": 10.0,
        "endY": 0.0,
        "widthCm": 2.0,
    }


def _v2_map(**overrides):
    payload = {
        "format": TABLE_MAP_FORMAT,
        "version": 2,
        "table": {"widthCm": 200, "heightCm": 100},
        "layers": [
            {"id": "ground", "name": "Ground", "zCm": 0, "lines": [_line()]},
        ],
        "transitions": [],
        "activeLayerId": "ground",
    }
    payload.update(overrides)
    return payload


def _v1_map():
    return {
        "format": TABLE_MAP_FORMAT,
        "version": 1,
        "table": {"widthCm": 200, "heightCm": 100},
        "lines": [_line()],
    }


# --------------------------------------------------------------------------- #
# parse_v2
# --------------------------------------------------------------------------- #


def test_parse_v2_accepts_valid_v2():
    result = parse_v2(_v2_map())
    assert result["version"] == TABLE_MAP_VERSION
    assert result["format"] == TABLE_MAP_FORMAT
    assert result["layers"][0]["id"] == "ground"
    assert result["activeLayerId"] == "ground"


def test_parse_v2_fills_layer_defaults():
    result = parse_v2(
        _v2_map(layers=[{"lines": [_line()]}, {"lines": []}])
    )
    assert result["layers"][0]["id"] == "layer-0"
    assert result["layers"][0]["name"] == "Layer 1"
    assert result["layers"][0]["zCm"] == 0
    assert result["layers"][1]["zCm"] == 10
    # activeLayerId falls back to the first layer when the given one is unknown.
    assert result["activeLayerId"] == "layer-0"


def test_parse_v2_rejects_v1():
    with pytest.raises(TableMapVersionError) as exc:
        parse_v2(_v1_map())
    assert "v1 was dropped" in str(exc.value)


def test_parse_v2_rejects_missing_version():
    payload = _v2_map()
    del payload["version"]
    with pytest.raises(TableMapVersionError):
        parse_v2(payload)


def test_parse_v2_rejects_wrong_format():
    with pytest.raises(TableMapVersionError):
        parse_v2(_v2_map(format="something-else"))


def test_parse_v2_rejects_missing_layers():
    payload = _v2_map()
    del payload["layers"]
    with pytest.raises(TableMapVersionError):
        parse_v2(payload)


def test_parse_v2_rejects_empty_layers():
    with pytest.raises(TableMapVersionError):
        parse_v2(_v2_map(layers=[]))


def test_parse_v2_rejects_non_dict():
    with pytest.raises(TableMapVersionError):
        parse_v2("not a map")
    with pytest.raises(TableMapVersionError):
        parse_v2(None)


def test_parse_v2_defaults_missing_transitions():
    payload = _v2_map()
    del payload["transitions"]
    assert parse_v2(payload)["transitions"] == []


# --------------------------------------------------------------------------- #
# TableMapRequest
# --------------------------------------------------------------------------- #


def test_request_accepts_v2_and_to_dict_is_canonical():
    req = TableMapRequest(**_v2_map())
    out = req.to_dict()
    assert out["version"] == 2
    assert out["format"] == TABLE_MAP_FORMAT
    assert out["layers"][0]["lines"][0]["kind"] == "line"
    assert out["activeLayerId"] == "ground"


def test_request_rejects_v1_version():
    with pytest.raises(ValidationError):
        TableMapRequest(**{**_v2_map(), "version": 1})


def test_request_rejects_v1_payload_without_layers():
    # A legacy v1 body has flat lines[] and no layers — must fail validation.
    with pytest.raises(ValidationError):
        TableMapRequest(**_v1_map())


def test_request_to_dict_fills_active_layer():
    req = TableMapRequest(**_v2_map(activeLayerId="nonexistent"))
    assert req.to_dict()["activeLayerId"] == "ground"


def test_request_supports_transitions_with_from_alias():
    payload = _v2_map(
        layers=[
            {"id": "a", "name": "A", "zCm": 0, "lines": []},
            {"id": "b", "name": "B", "zCm": 10, "lines": []},
        ],
        transitions=[
            {
                "id": "t1",
                "fromLayer": "a",
                "toLayer": "b",
                "from": {"startX": 0, "startY": 0, "endX": 5, "endY": 0},
                "to": {"startX": 0, "startY": 0, "endX": 5, "endY": 0},
            }
        ],
        activeLayerId="a",
    )
    req = TableMapRequest(**payload)
    out = req.to_dict()
    assert out["transitions"][0]["fromLayer"] == "a"
    assert out["transitions"][0]["from"]["endX"] == 5
