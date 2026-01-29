import ast
from pathlib import Path
from typing import List, Dict, Any, Optional, Set
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
class StepFunction:
    """Represents a DSL-decorated function"""
    name: str
    import_path: str
    arguments: List[StepArgument]
    file_path: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "import": self.import_path,
            "arguments": [arg.to_dict() for arg in self.arguments],
            "file": self.file_path
        }


class DSLStepAnalyzer:
    """Analyzes Python files to extract @dsl decorated functions and their signatures"""

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.discovered_steps: List[StepFunction] = []

    def analyze_all_steps(self) -> List[StepFunction]:
        """Analyze all Python files in the project for @dsl decorated functions"""
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
        """Find all step files in the libstp_helpers library"""
        step_files = []

        # Look for step files in libstp_helpers
        lib_dir = self.project_root / "libstp"
        if lib_dir.exists():
            step_files.extend(lib_dir.rglob("*.py"))

        return step_files

    def _analyze_file(self, file_path: Path):
        """Analyze a single Python file for @dsl decorated functions"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()

            if not content.strip():
                return

            # Parse with ast for traversal
            ast_tree = ast.parse(content)

            # Extract imports for type resolution
            imports = self._extract_imports(ast_tree)

            # Collect step classes to support the new factory style
            step_classes = self._collect_step_classes(ast_tree)

            # Find DSL functions or factory functions returning Step subclasses
            for node in ast.walk(ast_tree):
                if isinstance(node, ast.FunctionDef):
                    if self._is_step_function(node, step_classes, imports):
                        step_func = self._analyze_function(node, file_path, imports)
                        if step_func:
                            self.discovered_steps.append(step_func)

        except Exception as e:
            print(f"Error analyzing {file_path}: {e}")

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

    def _has_dsl_decorator(self, node: ast.FunctionDef) -> bool:
        """Check if function has @dsl decorator"""
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Name) and decorator.id == "dsl":
                return True
            elif isinstance(decorator, ast.Attribute) and decorator.attr == "dsl":
                return True
        return False

    def _collect_step_classes(self, tree: ast.AST) -> Set[str]:
        """Collect the names of classes inheriting from Step"""
        step_classes: Set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for base in node.bases:
                    base_name = self._get_node_name(base)
                    if base_name:
                        simple_name = base_name.split(".")[-1]
                        if simple_name == "Step":
                            step_classes.add(node.name)
                            break
        return step_classes

    def _is_step_function(self, node: ast.FunctionDef, step_classes: Set[str], imports: Dict[str, str]) -> bool:
        if self._has_dsl_decorator(node):
            return True
        return self._is_step_factory_function(node, step_classes, imports)

    def _is_step_factory_function(self, node: ast.FunctionDef, step_classes: Set[str], imports: Dict[str, str]) -> bool:
        if not step_classes:
            return False

        if node.returns:
            return_type, _, _ = self._resolve_type_annotation(node.returns, imports)
            if return_type in step_classes:
                return True

        for sub_node in ast.walk(node):
            if isinstance(sub_node, ast.Return):
                if self._is_step_constructor_call(sub_node.value, step_classes):
                    return True
        return False

    def _is_step_constructor_call(self, node: Optional[ast.AST], step_classes: Set[str]) -> bool:
        if not isinstance(node, ast.Call):
            return False

        constructor_name = self._get_node_name(node.func)
        if constructor_name:
            simple_name = constructor_name.split(".")[-1]
            return simple_name in step_classes
        return False

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

    def _analyze_function(self, node: ast.FunctionDef, file_path: Path, imports: Dict[str, str]) -> Optional[StepFunction]:
        """Analyze a @dsl decorated function to extract its signature"""
        try:
            # Generate import path
            import_path = self._generate_import_path(file_path)

            # Extract arguments
            arguments = []
            for arg in node.args.args:
                if arg.arg == "self":  # Skip self parameter
                    continue

                arg_info = self._analyze_argument(arg, node.args, imports)
                if arg_info:
                    arguments.append(arg_info)

            return StepFunction(
                name=node.name,
                import_path=f"{import_path}.{node.name}",
                arguments=arguments,
                file_path=str(file_path)
            )

        except Exception as e:
            print(f"Error analyzing function {node.name}: {e}")
            return None

    def _analyze_argument(self, arg: ast.arg, args: ast.arguments, imports: Dict[str, str]) -> Optional[StepArgument]:
        """Analyze a function argument to extract type information"""
        try:
            # Get argument name
            arg_name = arg.arg

            # Get type annotation
            type_name, type_import, is_optional = self._resolve_type_annotation(arg.annotation, imports)

            # Get default value
            default_value = self._get_default_value(arg_name, args)

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

    def _get_default_value(self, arg_name: str, args: ast.arguments) -> Optional[str]:
        """Get default value for an argument if it exists"""
        # Match argument position with defaults
        arg_names = [arg.arg for arg in args.args]
        if arg_name in arg_names:
            arg_index = arg_names.index(arg_name)
            # Defaults are for the last len(defaults) arguments
            defaults_start = len(arg_names) - len(args.defaults)
            if arg_index >= defaults_start:
                default_index = arg_index - defaults_start
                default_node = args.defaults[default_index]
                try:
                    if isinstance(default_node, ast.Constant):
                        return repr(default_node.value)
                    else:
                        return ast.unparse(default_node)
                except Exception:
                    return "..."
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
