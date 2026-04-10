"""Code generation pipeline for orchestrating multiple generators."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional

from .generators.registry import GeneratorRegistry
from .generators.defs_generator import DefsGenerator
from .generators.defs_stub_generator import DefsStubGenerator
from .generators.robot_generator import RobotGenerator

logger = logging.getLogger("raccoon")


class CodegenPipeline:
    """
    Orchestrates the code generation process.

    Manages the execution of multiple generators with proper
    logging, error handling, and progress reporting.
    """

    def __init__(self):
        """Initialize the pipeline."""
        self.registry = GeneratorRegistry()
        self._setup_default_generators()

    def _setup_default_generators(self) -> None:
        """Register default generators."""
        # Register hardware definitions generator
        self.registry.register("defs", DefsGenerator(class_name="Defs"))

        # Register type stub for IDE autocomplete (ServoPreset positions etc.)
        self.registry.register("defs_stub", DefsStubGenerator(class_name="Defs"))

        # Register robot configuration generator
        self.registry.register("robot", RobotGenerator(class_name="Robot"))

        logger.debug(
            f"Registered generators: {', '.join(self.registry.list_generators())}"
        )

    def run_all(
        self, config: dict, output_dir: Path, format_code: bool = True
    ) -> Dict[str, Path]:
        """
        Run all registered generators.

        Args:
            config: Full project configuration
            output_dir: Directory to write output files
            format_code: Whether to format code with black

        Returns:
            Dictionary mapping generator names to output file paths
        """
        logger.info("Starting code generation pipeline...")
        logger.info(f"Output directory: {output_dir}")

        try:
            results = self.registry.generate_all(config, output_dir, format_code)

            logger.info("✓ Code generation completed successfully")
            logger.info(f"Generated {len(results)} file(s):")
            for name, path in results.items():
                logger.info(f"  - {path.relative_to(output_dir.parent)}")

            return results

        except Exception as e:
            logger.error(f"✗ Code generation failed: {e}")
            raise

    def run_specific(
        self,
        generator_names: List[str],
        config: dict,
        output_dir: Path,
        format_code: bool = True,
    ) -> Dict[str, Path]:
        """
        Run specific generators by name.

        Args:
            generator_names: List of generator names to run
            config: Full project configuration
            output_dir: Directory to write output files
            format_code: Whether to format code with black

        Returns:
            Dictionary mapping generator names to output file paths
        """
        logger.info(f"Running generators: {', '.join(generator_names)}")
        logger.info(f"Output directory: {output_dir}")

        results = {}
        for name in generator_names:
            try:
                path = self.registry.generate_one(name, config, output_dir, format_code)
                results[name] = path
                logger.info(f"✓ Generated {path.relative_to(output_dir.parent)}")
            except KeyError as e:
                logger.error(str(e))
                raise
            except Exception as e:
                logger.error(f"✗ Failed to generate {name}: {e}")
                raise

        logger.info(f"✓ Generated {len(results)} file(s)")
        return results

    def list_generators(self) -> List[str]:
        """
        List available generator names.

        Returns:
            List of generator names
        """
        return self.registry.list_generators()


def create_pipeline() -> CodegenPipeline:
    """
    Factory function to create a configured pipeline.

    Returns:
        Configured CodegenPipeline instance
    """
    return CodegenPipeline()
