"""Shared table-map (``.ftmap``) schema and validation.

Single source of truth for the ``flowchart-table-map`` format used by the
Web-IDE, the IDE backend, the Pi server, codegen and the simulator. **Only
v2 is supported** — the legacy v1 (flat ``lines[]``) format was dropped.

A v2 map is a JSON document::

    {
      "format": "flowchart-table-map",
      "version": 2,
      "table": { "widthCm": ..., "heightCm": ... },
      "layers": [ { "id", "name", "zCm", "lines": [...] }, ... ],
      "transitions": [ ... ],
      "activeLayerId": "<layer id>"
    }

CLI commands and the IDE/Pi backends all go through this module — there is no
second implementation of the parsing/validation logic (see CLAUDE.md, "Einheitliche
Service-Schicht").
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


TABLE_MAP_FORMAT = "flowchart-table-map"
TABLE_MAP_VERSION = 2


class TableMapVersionError(ValueError):
    """Raised when a table map is not a supported v2 document.

    Covers wrong ``format``, missing/legacy ``version`` (v1 was dropped) and
    structurally invalid v2 payloads (e.g. no layers).
    """


# --------------------------------------------------------------------------- #
# Pydantic request models (v2 only)
# --------------------------------------------------------------------------- #


class TableMapLine(BaseModel):
    """A single line or wall segment on a layer."""

    kind: str  # 'line' or 'wall'
    startX: float
    startY: float
    endX: float
    endY: float
    widthCm: float


class TableMapTransitionEdge(BaseModel):
    startX: float
    startY: float
    endX: float
    endY: float


class TableMapTransition(BaseModel):
    """An inter-layer transition (ramp/portal) connecting two edges."""

    id: str
    name: Optional[str] = None
    fromLayer: str
    toLayer: str
    from_: TableMapTransitionEdge = Field(alias="from")
    to: TableMapTransitionEdge
    bidirectional: Optional[bool] = True
    costMultiplier: Optional[float] = 1.0
    widthCm: Optional[float] = None

    model_config = {"populate_by_name": True}


class TableMapLayer(BaseModel):
    """A single stacked level."""

    id: str
    name: str
    zCm: Optional[float] = None
    lines: list[TableMapLine]


class TableMapRequest(BaseModel):
    """Request body to set a table map. Accepts **only** canonical v2.

    ``layers`` is required — a legacy v1 payload (flat ``lines[]`` without
    ``layers``) fails validation, which is the intended hard v1 drop.
    ``to_dict()`` returns the canonical, defaults-filled v2 representation.
    """

    format: str = TABLE_MAP_FORMAT
    version: int = TABLE_MAP_VERSION
    table: dict  # { widthCm, heightCm }
    layers: list[TableMapLayer]
    transitions: list[TableMapTransition] = Field(default_factory=list)
    activeLayerId: Optional[str] = None

    @field_validator("version")
    @classmethod
    def _reject_non_v2(cls, value: int) -> int:
        if value != TABLE_MAP_VERSION:
            raise ValueError(
                f"unsupported table map version {value!r}; only v{TABLE_MAP_VERSION} "
                "is supported (v1 was dropped)"
            )
        return value

    def to_dict(self) -> dict:
        """Return the canonical v2 dict with defensive defaults filled in."""
        layers_payload = [
            {
                "id": layer.id,
                "name": layer.name,
                "zCm": layer.zCm if layer.zCm is not None else idx * 10,
                "lines": [line.model_dump() for line in layer.lines],
            }
            for idx, layer in enumerate(self.layers)
        ]
        transitions_payload = [
            t.model_dump(by_alias=True, exclude_none=False) for t in self.transitions
        ]
        active_id = self.activeLayerId
        if layers_payload and (
            not isinstance(active_id, str)
            or not any(l["id"] == active_id for l in layers_payload)
        ):
            active_id = layers_payload[0]["id"]

        return {
            "format": TABLE_MAP_FORMAT,
            "version": TABLE_MAP_VERSION,
            "table": self.table,
            "layers": layers_payload,
            "transitions": transitions_payload,
            "activeLayerId": active_id,
        }


# --------------------------------------------------------------------------- #
# Dict-level validation (used by codegen, simulation, on-disk reads)
# --------------------------------------------------------------------------- #


def parse_v2(payload: Any) -> dict:
    """Validate and normalize a raw table-map dict to canonical v2.

    Raises :class:`TableMapVersionError` for anything that is not a well-formed
    v2 document — including legacy v1 (flat ``lines[]``) payloads, since v1
    support has been dropped.
    """
    if not isinstance(payload, dict):
        raise TableMapVersionError("table map must be a JSON object")

    fmt = payload.get("format")
    if fmt != TABLE_MAP_FORMAT:
        raise TableMapVersionError(
            f"unsupported table map format {fmt!r} (expected {TABLE_MAP_FORMAT!r})"
        )

    version = payload.get("version")
    if version != TABLE_MAP_VERSION:
        raise TableMapVersionError(
            f"unsupported table map version {version!r}; only v{TABLE_MAP_VERSION} "
            "is supported (v1 was dropped)"
        )

    raw_layers = payload.get("layers")
    if not isinstance(raw_layers, list) or not raw_layers:
        raise TableMapVersionError(
            "v2 table map requires a non-empty 'layers' list"
        )

    table = payload.get("table") or {"widthCm": 0, "heightCm": 0}

    layers: list[dict] = []
    for idx, raw_layer in enumerate(raw_layers):
        if not isinstance(raw_layer, dict):
            continue
        z_cm = raw_layer.get("zCm")
        layers.append(
            {
                "id": raw_layer.get("id") or f"layer-{idx}",
                "name": raw_layer.get("name") or f"Layer {idx + 1}",
                "zCm": z_cm if isinstance(z_cm, (int, float)) else idx * 10,
                "lines": raw_layer.get("lines")
                if isinstance(raw_layer.get("lines"), list)
                else [],
            }
        )
    if not layers:
        raise TableMapVersionError("v2 table map requires at least one valid layer")

    active_id = payload.get("activeLayerId")
    if not isinstance(active_id, str) or not any(l["id"] == active_id for l in layers):
        active_id = layers[0]["id"]

    transitions = payload.get("transitions")
    if not isinstance(transitions, list):
        transitions = []

    return {
        "format": TABLE_MAP_FORMAT,
        "version": TABLE_MAP_VERSION,
        "table": table,
        "layers": layers,
        "transitions": transitions,
        "activeLayerId": active_id,
    }
