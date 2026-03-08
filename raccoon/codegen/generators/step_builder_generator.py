"""Step builder generator — thin wrapper around libstp.codegen.

The actual code generation logic lives in the libstp library at
``libstp.codegen.step_builder_gen``.  This module re-exports the
public API so the raccoon toolchain can use it without duplicating code.

Usage from raccoon::

    from raccoon.codegen.generators.step_builder_generator import (
        generate_step_builders,
    )

    results = generate_step_builders(source_dir)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List

from libstp.codegen.step_builder_gen import (
    ParamInfo,
    StepClassInfo,
    scan_file,
    generate_file,
    generate_for_source_dirs,
    camel_to_snake,
)

logger = logging.getLogger("raccoon")

# Re-export everything the toolchain might need
__all__ = [
    "ParamInfo",
    "StepClassInfo",
    "scan_file",
    "generate_file",
    "generate_step_builders",
    "camel_to_snake",
]


def generate_step_builders(
    source_dir: Path,
    *,
    dry_run: bool = False,
    format_code: bool = True,
) -> Dict[Path, str]:
    """Convenience wrapper matching the original raccoon API.

    Args:
        source_dir: Root directory to scan for ``@dsl_step`` classes.
        dry_run: Preview without writing files.
        format_code: Format output with ``black`` if available.

    Returns:
        Mapping of output file paths to generated source code.
    """
    logger.info("Scanning %s for @dsl_step classes...", source_dir)
    results = generate_for_source_dirs(
        [source_dir], dry_run=dry_run, format_code=format_code
    )
    logger.info(
        "Found %d @dsl_step class(es) → %d file(s)",
        sum(code.count("class ") for code in results.values()),
        len(results),
    )
    return results
