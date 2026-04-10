"""Discover DSL-decorated step functions and classes from Python source."""

import ast
import textwrap
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass


@dataclass
class StepArgument:
    """Represents an argument of a DSL function"""
    name: str
    type_name: str
    type_import: Optional[str]
    is_optional: bool
    default_value: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type_name,
            "import": self.type_import,
            "optional": self.is_optional,
            "default": self.default_value
        }


@dataclass
class StepChainMethod:
    """Represents a chainable method that can be appended to a step builder."""

    name: str
    arguments: List[StepArgument]
    chain_methods: List['StepChainMethod'] | None = None
    recursive: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "arguments": [arg.to_dict() for arg in self.arguments],
            "chain_methods": [method.to_dict() for method in (self.chain_methods or [])],
            "recursive": self.recursive,
        }


@dataclass
class StepFunction:
    """Represents a DSL-decorated function or class"""
    name: str
    import_path: str
    arguments: List[StepArgument]
    file_path: str
    tags: List[str] | None = None
    chain_methods: List[StepChainMethod] | None = None
    docstring: Optional[str] = None
    signature: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "name": self.name,
            "import": self.import_path,
            "arguments": [arg.to_dict() for arg in self.arguments],
            "file": self.file_path,
            "tags": self.tags or [],
            "chain_methods": [method.to_dict() for method in (self.chain_methods or [])],
        }
        if self.docstring is not None:
            d["docstring"] = self.docstring
        if self.signature is not None:
            d["signature"] = self.signature
        return d


class DSLStepAnalyzer:
    """Analyzes Python files to extract @dsl decorated functions/classes and their signatures"""

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.discovered_steps: List[StepFunction] = []

    def analyze_all_steps(self) -> List[StepFunction]:
        """Analyze all Python files in the project for @dsl decorated functions/classes"""
        self.discovered_steps = []

        # Analyze project-specific steps
        project_steps = self._find_project_steps()
        for step_file in project_steps:
            self._analyze_file(step_file)

        # Analyze library steps
        library_steps = self._find_library_steps()
        for step_file in library_steps:
            self._analyze_file(step_file)

        return self.discovered_steps

    def _find_project_steps(self) -> List[Path]:
        """Find all step files in project directories"""
        step_files = []

        # Look for step files in projects directory
        projects_dir = self.project_root / "projects"
        if projects_dir.exists():
            step_files.extend(projects_dir.rglob("*step*.py"))

        return step_files

    def _find_library_steps(self) -> List[Path]:
        """Find all step files in the libstp library.

        Prefer ``.py`` over ``.pyi`` when both exist for the same module so we
        don't emit duplicate steps from stub/runtime pairs.
        """
        lib_dir = self.project_root / "libstp"
        if lib_dir.exists():
            module_files: Dict[Path, Path] = {}
            for pattern in ("*.pyi", "*.py"):
                for path in lib_dir.rglob(pattern):
                    if "__pycache__" in path.parts:
                        continue
                    module_key = path.relative_to(lib_dir).with_suffix("")
                    module_files[module_key] = path

            return list(module_files.values())

        return []

    def _analyze_file(self, file_path: Path):
        """Analyze a single Python file for @dsl decorated functions/classes"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()

            if not content.strip():
                return

            # Parse with ast for traversal
            ast_tree = ast.parse(content)

            # Extract imports for type resolution
            imports = self._extract_imports(ast_tree)

            # Find @dsl decorated functions and classes
            found_dsl = False
            for node in ast.walk(ast_tree):
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                    if self._is_dsl_step(node):
                        step_func = self._analyze_dsl_node(node, file_path, imports)
                        if step_func:
                            self.discovered_steps.append(step_func)
                            found_dsl = True

            # Fallback: for *_dsl.pyi stubs without @dsl decorator,
            # extract DSL functions by naming convention
            if not found_dsl and file_path.name.endswith('_dsl.pyi'):
                self._analyze_stub_file(ast_tree, file_path, imports)

        except Exception as e:
            print(f"Error analyzing {file_path}: {e}")

    def _analyze_stub_file(self, tree: ast.Module, file_path: Path, imports: Dict[str, str]):
        """Extract DSL steps from stub (.pyi) files without @dsl decorator.

        Stub files follow the pattern: Builder class + DSL factory function.
        The factory function uses (*args, **kwargs) but has a rich docstring.
        We reconstruct the signature from the builder class methods.
        """
        # Collect top-level nodes
        builders: Dict[str, ast.ClassDef] = {}
        functions: List[ast.FunctionDef] = []

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef) and node.name.endswith('Builder'):
                builders[node.name] = node
            elif isinstance(node, ast.FunctionDef) and not node.name.startswith('_'):
                functions.append(node)

        import_path = self._generate_import_path(file_path)

        for func in functions:
            docstring_raw = ast.get_docstring(func)
            if not docstring_raw:
                continue

            docstring = textwrap.dedent(docstring_raw).strip()

            # Try to find matching builder class
            # e.g. calibrate → CalibrateBuilder
            builder_name = func.name.replace('_', ' ').title().replace(' ', '') + 'Builder'
            builder = builders.get(builder_name)

            # Reconstruct signature from builder methods
            signature = self._reconstruct_signature_from_builder(func.name, builder, docstring)

            # Extract arguments from builder or docstring
            arguments = self._extract_args_from_builder(builder, imports) if builder else []

            # Extract tags from docstring module path
            tags = self._infer_tags_from_path(file_path)

            step = StepFunction(
                name=func.name,
                import_path=f"{import_path}.{func.name}",
                arguments=arguments,
                file_path=str(file_path),
                tags=tags or None,
                chain_methods=self._infer_chain_methods(func.name, tags or []),
                docstring=docstring,
                signature=signature,
            )
            self.discovered_steps.append(step)

    def _reconstruct_signature_from_builder(self, func_name: str, builder: Optional[ast.ClassDef], docstring: str) -> str:
        """Reconstruct a typed function signature from builder methods and docstring."""
        if not builder:
            # Parse params from docstring Args section
            return self._signature_from_docstring(func_name, docstring)

        params: List[str] = []
        for node in ast.iter_child_nodes(builder):
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name.startswith('_') or node.name in ('__init__',):
                continue
            # Builder methods have pattern: def method_name(self, value: Type)
            method_args = node.args.args[1:]  # skip self
            if len(method_args) == 1:
                arg = method_args[0]
                annotation = f": {ast.unparse(arg.annotation)}" if arg.annotation else ""
                # Check for default in docstring
                default = self._find_default_in_docstring(node.name, docstring)
                if default:
                    params.append(f"{node.name}{annotation} = {default}")
                else:
                    params.append(f"{node.name}{annotation}")

        return f"{func_name}({', '.join(params)})"

    def _signature_from_docstring(self, func_name: str, docstring: str) -> str:
        """Build a minimal signature from the docstring Args section."""
        params: List[str] = []
        in_args = False
        for line in docstring.split('\n'):
            trimmed = line.strip()
            if trimmed == 'Args:':
                in_args = True
                continue
            if in_args:
                if trimmed.startswith(('Returns:', 'Example')):
                    break
                m = line.lstrip()
                if m and not m[0].isspace() and ':' in m:
                    param_name = m.split(':')[0].strip()
                    if param_name and param_name.isidentifier():
                        params.append(param_name)
        return f"{func_name}({', '.join(params)})"

    def _find_default_in_docstring(self, param_name: str, docstring: str) -> Optional[str]:
        """Try to find a default value mentioned in the docstring for a parameter."""
        # Look for patterns like "param_name: ... Defaults to X" or "param_name: ... (default: X)"
        # This is a heuristic — not always available
        return None

    def _extract_args_from_builder(self, builder: ast.ClassDef, imports: Dict[str, str]) -> List[StepArgument]:
        """Extract step arguments from a builder class's setter methods."""
        args: List[StepArgument] = []
        for node in ast.iter_child_nodes(builder):
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name.startswith('_'):
                continue
            method_args = node.args.args[1:]  # skip self
            if len(method_args) == 1:
                arg = method_args[0]
                type_name, type_import, is_optional = self._resolve_type_annotation(arg.annotation, imports)
                args.append(StepArgument(
                    name=node.name,
                    type_name=type_name,
                    type_import=type_import,
                    is_optional=is_optional,
                    default_value=None,
                ))
        return args

    def _infer_tags_from_path(self, file_path: Path) -> List[str]:
        """Infer tags from the file's directory structure."""
        parts = file_path.parts
        tags: List[str] = []
        # Look for known category directories
        step_idx = None
        for i, part in enumerate(parts):
            if part == 'step':
                step_idx = i
                break
        if step_idx is not None and step_idx + 1 < len(parts):
            category = parts[step_idx + 1]
            if category not in ('__pycache__',) and not category.endswith(('.py', '.pyi')):
                tags.append(category)
        return tags

    def _extract_imports(self, tree: ast.AST) -> Dict[str, str]:
        """Extract import statements to resolve type annotations"""
        imports = {}

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.asname if alias.asname else alias.name
                    imports[name] = alias.name

            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    for alias in node.names:
                        name = alias.asname if alias.asname else alias.name
                        imports[name] = f"{node.module}.{alias.name}"

        return imports

    def _has_dsl_decorator(self, node: ast.FunctionDef | ast.ClassDef) -> bool:
        """Check if function or class has @dsl decorator"""
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Name) and decorator.id == "dsl":
                return True
            elif isinstance(decorator, ast.Attribute) and decorator.attr == "dsl":
                return True
            elif isinstance(decorator, ast.Call):
                func_name = self._get_node_name(decorator.func)
                if func_name and func_name.split(".")[-1] == "dsl":
                    return True
        return False

    def _get_dsl_call_decorator(self, node: ast.FunctionDef | ast.ClassDef) -> Optional[ast.Call]:
        """Get the @dsl(...) call decorator if present"""
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Call):
                func_name = self._get_node_name(decorator.func)
                if func_name and func_name.split(".")[-1] == "dsl":
                    return decorator
        return None

    def _get_dsl_tags(self, node: ast.FunctionDef | ast.ClassDef) -> List[str]:
        """Extract tags from @dsl(tags=[...])"""
        tags: List[str] = []
        decorator = self._get_dsl_call_decorator(node)
        if not decorator:
            return tags
        for keyword in decorator.keywords:
            if keyword.arg != "tags":
                continue
            value = keyword.value
            if isinstance(value, (ast.List, ast.Tuple)):
                for elt in value.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        tags.append(elt.value)
        return tags

    def _get_dsl_name(self, node: ast.FunctionDef | ast.ClassDef) -> Optional[str]:
        """Extract custom name from @dsl(name="...")"""
        decorator = self._get_dsl_call_decorator(node)
        if not decorator:
            return None
        for keyword in decorator.keywords:
            if keyword.arg == "name":
                if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                    return keyword.value.value
        return None

    def _is_dsl_hidden(self, node: ast.FunctionDef | ast.ClassDef) -> bool:
        """Check if @dsl(hidden=True)"""
        decorator = self._get_dsl_call_decorator(node)
        if not decorator:
            return False
        for keyword in decorator.keywords:
            if keyword.arg == "hidden":
                if isinstance(keyword.value, ast.Constant):
                    return bool(keyword.value.value)
        return False

    def _is_dsl_step(self, node: ast.FunctionDef | ast.ClassDef) -> bool:
        """Only index functions/classes with @dsl decorator (and not hidden)"""
        if not self._has_dsl_decorator(node):
            return False
        if self._is_dsl_hidden(node):
            return False
        return True

    def _get_node_name(self, node: Optional[ast.AST]) -> Optional[str]:
        if node is None:
            return None

        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            parts = []
            current = node
            while isinstance(current, ast.Attribute):
                parts.append(current.attr)
                current = current.value
            if isinstance(current, ast.Name):
                parts.append(current.id)
            return ".".join(reversed(parts))
        if isinstance(node, ast.Subscript):
            return self._get_node_name(node.value)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    @staticmethod
    def _format_function_signature(node: ast.FunctionDef) -> str:
        """Build a human-readable function signature string."""
        args = node.args
        parts: list[str] = []

        all_args = args.args
        defaults = args.defaults
        n_no_default = len(all_args) - len(defaults)

        for i, arg in enumerate(all_args):
            if arg.arg in ("self", "cls"):
                continue
            annotation = ""
            if arg.annotation:
                annotation = f": {ast.unparse(arg.annotation)}"
            if i >= n_no_default:
                default = ast.unparse(defaults[i - n_no_default])
                parts.append(f"{arg.arg}{annotation} = {default}")
            else:
                parts.append(f"{arg.arg}{annotation}")

        for i, arg in enumerate(args.kwonlyargs):
            annotation = ""
            if arg.annotation:
                annotation = f": {ast.unparse(arg.annotation)}"
            default = args.kw_defaults[i]
            if default is not None:
                parts.append(f"{arg.arg}{annotation} = {ast.unparse(default)}")
            else:
                parts.append(f"{arg.arg}{annotation}")

        ret = ""
        if node.returns:
            ret = f" -> {ast.unparse(node.returns)}"

        return f"{node.name}({', '.join(parts)}){ret}"

    def _analyze_dsl_node(self, node: ast.FunctionDef | ast.ClassDef, file_path: Path, imports: Dict[str, str]) -> Optional[StepFunction]:
        """Analyze a @dsl decorated function or class to extract its signature"""
        try:
            tags = self._get_dsl_tags(node)
            custom_name = self._get_dsl_name(node)
            import_path = self._generate_import_path(file_path)

            # Extract arguments from function or class __init__
            arguments = []
            if isinstance(node, ast.FunctionDef):
                args_node = node.args
            else:
                # For classes, find __init__ method
                args_node = self._get_class_init_args(node)

            if args_node:
                for arg in args_node.args:
                    if arg.arg == "self":
                        continue
                    arg_info = self._analyze_argument(arg, args_node, imports)
                    if arg_info:
                        arguments.append(arg_info)

            step_name = custom_name if custom_name else node.name

            # Extract docstring
            raw_docstring = ast.get_docstring(node)
            docstring = textwrap.dedent(raw_docstring).strip() if raw_docstring else None

            # Extract full signature for functions
            signature = None
            if isinstance(node, ast.FunctionDef):
                try:
                    signature = self._format_function_signature(node)
                except Exception:
                    pass

            return StepFunction(
                name=step_name,
                import_path=f"{import_path}.{node.name}",
                arguments=arguments,
                file_path=str(file_path),
                tags=tags or None,
                chain_methods=self._infer_chain_methods(step_name, tags or []),
                docstring=docstring,
                signature=signature,
            )

        except Exception as e:
            print(f"Error analyzing {node.name}: {e}")
            return None

    def _get_class_init_args(self, node: ast.ClassDef) -> Optional[ast.arguments]:
        """Extract arguments from a class's __init__ method"""
        for item in node.body:
            if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                return item.args
        return None

    def _analyze_argument(self, arg: ast.arg, args: ast.arguments, imports: Dict[str, str]) -> Optional[StepArgument]:
        """Analyze a function argument to extract type information"""
        try:
            # Get argument name
            arg_name = arg.arg

            # Get type annotation
            type_name, type_import, is_optional = self._resolve_type_annotation(arg.annotation, imports)

            default_node = self._get_default_node(arg_name, args)
            if type_name == "Any" and default_node is not None:
                inferred_type = self._infer_type_from_default(default_node)
                if inferred_type is not None:
                    type_name = inferred_type

            # Get default value
            default_value = self._format_default_value(default_node)

            return StepArgument(
                name=arg_name,
                type_name=type_name,
                type_import=type_import,
                is_optional=is_optional or default_value is not None,
                default_value=default_value
            )

        except Exception as e:
            print(f"Error analyzing argument {arg.arg}: {e}")
            return None

    def _resolve_type_annotation(self, annotation: ast.AST, imports: Dict[str, str]) -> tuple[str, Optional[str], bool]:
        """Resolve type annotation to type name, import path, and optional flag"""
        if not annotation:
            return "Any", None, False

        # Handle different annotation types
        if isinstance(annotation, ast.Name):
            type_name = annotation.id
            # Check for basic types
            if type_name in ["int", "float", "bool", "str", "list", "dict", "Any"]:
                return type_name, None, False
            else:
                # Look up import
                type_import = imports.get(type_name)
                return type_name, type_import, False

        elif isinstance(annotation, ast.Constant):
            return str(annotation.value), None, False

        elif isinstance(annotation, ast.Subscript):
            # Handle generic types like Optional[T], Union[T, None], List[T]
            if isinstance(annotation.value, ast.Name):
                outer_type = annotation.value.id

                if outer_type == "Optional":
                    # Optional[T] -> T, optional=True
                    inner_type, inner_import, _ = self._resolve_type_annotation(annotation.slice, imports)
                    return inner_type, inner_import, True

                elif outer_type == "Union":
                    # Union[T, None] -> T, optional=True (if None is present)
                    if isinstance(annotation.slice, ast.Tuple):
                        types = []
                        has_none = False
                        for elt in annotation.slice.elts:
                            if isinstance(elt, ast.Constant) and elt.value is None:
                                has_none = True
                            else:
                                type_name, type_import, _ = self._resolve_type_annotation(elt, imports)
                                types.append((type_name, type_import))

                        if len(types) == 1 and has_none:
                            return types[0][0], types[0][1], True
                        else:
                            # Multiple non-None types
                            type_names = [t[0] for t in types]
                            return f"Union[{', '.join(type_names)}]", None, has_none

                elif outer_type in ["List", "Dict", "Tuple"]:
                    # Generic collection types
                    return outer_type, "typing", False

        elif isinstance(annotation, ast.Attribute):
            # Handle module.Type annotations
            if isinstance(annotation.value, ast.Name):
                module = annotation.value.id
                type_name = annotation.attr
                type_import = imports.get(module, module)
                return type_name, f"{type_import}.{type_name}" if type_import else None, False

        # Fallback - convert to string representation
        try:
            return ast.unparse(annotation), None, False
        except Exception:
            return "Any", None, False

    def _get_default_node(self, arg_name: str, args: ast.arguments) -> Optional[ast.AST]:
        """Get the AST node for an argument default if it exists."""
        # Match argument position with defaults
        arg_names = [arg.arg for arg in args.args]
        if arg_name in arg_names:
            arg_index = arg_names.index(arg_name)
            # Defaults are for the last len(defaults) arguments
            defaults_start = len(arg_names) - len(args.defaults)
            if arg_index >= defaults_start:
                default_index = arg_index - defaults_start
                return args.defaults[default_index]
        return None

    def _format_default_value(self, default_node: Optional[ast.AST]) -> Optional[str]:
        """Convert a default AST node into the serialized value used by the IDE."""
        if default_node is None:
            return None
        try:
            if isinstance(default_node, ast.Constant):
                return repr(default_node.value)
            return ast.unparse(default_node)
        except Exception:
            return "..."

    def _infer_type_from_default(self, default_node: ast.AST) -> Optional[str]:
        """Infer a simple scalar type from a literal default value."""
        if isinstance(default_node, ast.Constant):
            value = default_node.value
            if isinstance(value, bool):
                return "bool"
            if isinstance(value, int):
                return "int"
            if isinstance(value, float):
                return "float"
            if isinstance(value, str):
                return "str"
            return None

        if isinstance(default_node, ast.UnaryOp) and isinstance(default_node.op, (ast.UAdd, ast.USub)):
            operand = default_node.operand
            if isinstance(operand, ast.Constant):
                value = operand.value
                if isinstance(value, bool):
                    return None
                if isinstance(value, int):
                    return "int"
                if isinstance(value, float):
                    return "float"

        return None

    def _generate_import_path(self, file_path: Path) -> str:
        """Generate Python import path from file path"""
        try:
            # Make path relative to project root
            relative_path = file_path.relative_to(self.project_root)

            # Convert path to module notation
            parts = list(relative_path.parts[:-1])  # Remove filename
            if relative_path.stem != "__init__":
                parts.append(relative_path.stem)

            return ".".join(parts)

        except ValueError:
            # File is not under project root
            return str(file_path.stem)

    def _infer_chain_methods(self, step_name: str, tags: List[str]) -> List[StepChainMethod] | None:
        normalized_name = (step_name or "").strip().lower()
        normalized_tags = {tag.strip().lower() for tag in (tags or []) if isinstance(tag, str)}

        methods: List[StepChainMethod] = []

        if "follow_line_single" in normalized_name:
            methods.append(
                StepChainMethod(
                    name="until",
                    arguments=[StepArgument("condition", "str", None, False, None)],
                    recursive=True,
                )
            )
            methods.append(
                StepChainMethod(
                    name="distance_cm",
                    arguments=[StepArgument("distance_cm", "float", None, False, None)],
                )
            )
            return methods

        is_motion_like = (
            "motion" in normalized_tags or
            "line-follow" in normalized_tags or
            any(token in normalized_name for token in ("drive", "strafe", "follow_line"))
        )
        if is_motion_like:
            methods.append(
                StepChainMethod(
                    name="until",
                    arguments=[StepArgument("condition", "str", None, False, None)],
                    recursive=True,
                )
            )

        return methods or None
