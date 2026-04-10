"""Step discovery endpoint for the Pi server.

Scans installed raccoon package for @dsl-decorated steps and returns them
so the IDE backend can cache them locally.
"""

import importlib
import logging
from pathlib import Path
from typing import List, Dict, Any

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/steps", tags=["steps"])


def _find_raccoon_path() -> Path | None:
    """Locate the installed raccoon package directory."""
    try:
        spec = importlib.util.find_spec("raccoon")
        if spec and spec.origin:
            return Path(spec.origin).parent
    except Exception:
        pass
    return None


@router.get("", response_model=List[Dict[str, Any]])
async def get_device_steps() -> List[Dict[str, Any]]:
    """Return all @dsl-decorated steps found in the raccoon package."""
    from raccoon_cli.ide.core.analysis.step_analyzer import DSLStepAnalyzer

    raccoon_path = _find_raccoon_path()
    if not raccoon_path:
        logger.warning("raccoon package not found – returning empty step list")
        return []

    # Point the analyzer at the parent of raccoon so it finds raccoon/ as a subdirectory
    analyzer = DSLStepAnalyzer(project_root=raccoon_path.parent)
    steps = analyzer.analyze_all_steps()

    logger.info(f"Discovered {len(steps)} steps from raccoon at {raccoon_path}")
    return [step.to_dict() for step in steps]
