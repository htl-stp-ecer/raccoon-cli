"""Generator registry for managing multiple code generators."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List

from .base import BaseGenerator

logger = logging.getLogger("raccoon")


class GeneratorRegistry:
    """
    Registry for managing and running multiple code generators.

    Provides centralized management of all generators and orchestrates
    their execution.
    """

    def __init__(self):
        self._generators: Dict[str, BaseGenerator] = {}

    def register(self, name: str, generator: BaseGenerator) -> None:
        """
        Register a generator.

        Args:
            name: Unique name for the generator
            generator: Generator instance to register
        """
        if name in self._generators:
            logger.warning(f"Overwriting existing generator: {name}")
        self._generators[name] = generator
        logger.debug(f"Registered generator: {name}")

    def get(self, name: str) -> BaseGenerator:
        """
        Get a generator by name.

        Args:
            name: Generator name

        Returns:
            Generator instance

        Raises:
            KeyError: If generator not found
        """
        if name not in self._generators:
            available = ", ".join(self._generators.keys())
            raise KeyError(
                f"Generator '{name}' not found. Available generators: {available}"
            )
        return self._generators[name]

    def list_generators(self) -> List[str]:
        """Return list of registered generator names."""
        return list(self._generators.keys())

    def generate_all(
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
        results = {}
        for name, generator in self._generators.items():
            try:
                output_file = generator.write(config, output_dir, format_code)
                results[name] = output_file
            except Exception as e:
                logger.error(f"Failed to generate {name}: {e}")
                raise

        return results

    def generate_one(
        self, name: str, config: dict, output_dir: Path, format_code: bool = True
    ) -> Path:
        """
        Run a specific generator by name.

        Args:
            name: Generator name
            config: Full project configuration
            output_dir: Directory to write output file
            format_code: Whether to format code with black

        Returns:
            Path to output file
        """
        generator = self.get(name)
        return generator.write(config, output_dir, format_code)
