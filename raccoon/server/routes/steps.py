"""Step discovery endpoint for the Pi server.

Scans installed libstp package for @dsl-decorated steps and returns them
so the IDE backend can cache them locally.
"""

import importlib
import logging
from pathlib import Path
from typing import List, Dict, Any

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/steps", tags=["steps"])


def _find_libstp_path() -> Path | None:
    """Locate the installed libstp package directory."""
    try:
        spec = importlib.util.find_spec("libstp")
        if spec and spec.origin:
            return Path(spec.origin).parent
    except Exception:
        pass
    return None


@router.get("", response_model=List[Dict[str, Any]])
async def get_device_steps() -> List[Dict[str, Any]]:
    """Return all @dsl-decorated steps found in the libstp package."""
    from raccoon.ide.core.analysis.step_analyzer import DSLStepAnalyzer

    libstp_path = _find_libstp_path()
    if not libstp_path:
        logger.warning("libstp package not found – returning empty step list")
        return []

    # Point the analyzer at the parent of libstp so it finds libstp/ as a subdirectory
    analyzer = DSLStepAnalyzer(project_root=libstp_path.parent)
    steps = analyzer.analyze_all_steps()

    logger.info(f"Discovered {len(steps)} steps from libstp at {libstp_path}")
    return [step.to_dict() for step in steps]
