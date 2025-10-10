"""Helper for building Python class definitions."""

from __future__ import annotations

from typing import List, Tuple


class ClassBuilder:
    """
    Builder for constructing Python class definitions.

    Provides a fluent interface for building class definitions with
    attributes, methods, and proper formatting.
    """

    def __init__(self, class_name: str):
        """
        Initialize the class builder.

        Args:
            class_name: Name of the class to build
        """
        self.class_name = class_name
        self._class_attrs: List[Tuple[str, str]] = []
        self._instance_attrs: List[Tuple[str, str, str]] = []

    def add_class_attribute(self, name: str, expression: str) -> ClassBuilder:
        """
        Add a class-level attribute.

        Args:
            name: Attribute name
            expression: Python expression for the value

        Returns:
            Self for chaining
        """
        if not name.isidentifier():
            raise ValueError(f"'{name}' is not a valid Python identifier")
        self._class_attrs.append((name, expression))
        return self

    def add_instance_attribute(
        self, name: str, type_hint: str, expression: str
    ) -> ClassBuilder:
        """
        Add an instance attribute (typically in __init__).

        Args:
            name: Attribute name
            type_hint: Type hint string
            expression: Python expression for the value

        Returns:
            Self for chaining
        """
        if not name.isidentifier():
            raise ValueError(f"'{name}' is not a valid Python identifier")
        self._instance_attrs.append((name, type_hint, expression))
        return self

    def build(self) -> str:
        """
        Build the class definition.

        Returns:
            Python class definition as a string
        """
        lines = []
        lines.append(f"class {self.class_name}:")

        # If no attributes, just pass
        if not self._class_attrs and not self._instance_attrs:
            lines.append("    pass")
            return "\n".join(lines)

        # Add class attributes
        if self._class_attrs:
            for name, expr in self._class_attrs:
                lines.append(f"    {name} = {expr}")

        # Add instance attributes if any
        if self._instance_attrs:
            if self._class_attrs:
                lines.append("")  # Blank line between class and instance attrs
            lines.append("    def __init__(self):")
            for name, type_hint, expr in self._instance_attrs:
                lines.append(f"        self.{name}: {type_hint} = {expr}")

        return "\n".join(lines)

    @staticmethod
    def build_simple_class(class_name: str, attributes: List[Tuple[str, str]]) -> str:
        """
        Build a simple class with only class-level attributes.

        Args:
            class_name: Name of the class
            attributes: List of (name, expression) tuples

        Returns:
            Python class definition as a string
        """
        builder = ClassBuilder(class_name)
        for name, expr in attributes:
            builder.add_class_attribute(name, expr)
        return builder.build()
