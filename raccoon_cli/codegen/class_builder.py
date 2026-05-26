"""Helper for building Python class definitions."""

from __future__ import annotations

import ast
from typing import List, Tuple


class ClassBuilder:
    """
    Builder for constructing Python class definitions.

    Provides a fluent interface for building class definitions with
    attributes, methods, and proper formatting.
    """

    def __init__(self, class_name: str, base_classes: List[str] = None):
        self.class_name = class_name
        self.base_classes = base_classes or []
        self._class_attrs: List[Tuple[str, str]] = []
        self._instance_attrs: List[Tuple[str, str, str]] = []
        self._methods: List[ast.FunctionDef] = []

    def add_class_attribute(self, name: str, expression: str) -> ClassBuilder:
        """Add a class-level attribute."""
        if not name.isidentifier():
            raise ValueError(f"'{name}' is not a valid Python identifier")
        self._class_attrs.append((name, expression))
        return self

    def add_instance_attribute(
        self, name: str, type_hint: str, expression: str
    ) -> ClassBuilder:
        """Add an instance attribute (typically in __init__)."""
        if not name.isidentifier():
            raise ValueError(f"'{name}' is not a valid Python identifier")
        self._instance_attrs.append((name, type_hint, expression))
        return self

    def add_method(self, source: str) -> ClassBuilder:
        """Add a method from a source string (def ...)."""
        node = ast.parse(source).body[0]
        if not isinstance(node, ast.FunctionDef):
            raise ValueError("Method source must define a function")
        self._methods.append(node)
        return self

    def build(self) -> str:
        """Build the class definition as a Python source string."""
        body: list[ast.stmt] = []

        for name, expr_str in self._class_attrs:
            value = ast.parse(expr_str, mode="eval").body
            stmt = ast.Assign(
                targets=[ast.Name(id=name, ctx=ast.Store())],
                value=value,
                lineno=0,
                col_offset=0,
            )
            body.append(stmt)

        if self._instance_attrs:
            init_body: list[ast.stmt] = []
            for attr_name, type_hint_str, expr_str in self._instance_attrs:
                annotation = ast.parse(type_hint_str, mode="eval").body
                value = ast.parse(expr_str, mode="eval").body
                stmt = ast.AnnAssign(
                    target=ast.Attribute(
                        value=ast.Name(id="self", ctx=ast.Load()),
                        attr=attr_name,
                        ctx=ast.Store(),
                    ),
                    annotation=annotation,
                    value=value,
                    simple=0,
                )
                init_body.append(stmt)
            init_fn = ast.FunctionDef(
                name="__init__",
                args=ast.arguments(
                    posonlyargs=[],
                    args=[ast.arg(arg="self")],
                    vararg=None,
                    kwonlyargs=[],
                    kw_defaults=[],
                    kwarg=None,
                    defaults=[],
                ),
                body=init_body,
                decorator_list=[],
                returns=None,
            )
            body.append(init_fn)

        if self._methods:
            body.extend(self._methods)

        if not body:
            body = [ast.Pass()]

        bases = [ast.Name(id=b, ctx=ast.Load()) for b in self.base_classes]
        class_def = ast.ClassDef(
            name=self.class_name,
            bases=bases,
            keywords=[],
            body=body,
            decorator_list=[],
        )
        module = ast.Module(body=[class_def], type_ignores=[])
        ast.fix_missing_locations(module)
        return ast.unparse(module)

    @staticmethod
    def build_simple_class(class_name: str, attributes: List[Tuple[str, str]]) -> str:
        """Build a simple class with only class-level attributes."""
        builder = ClassBuilder(class_name)
        for name, expr in attributes:
            builder.add_class_attribute(name, expr)
        return builder.build()
