"""Generate mission Python source code from parsed IDE mission models."""

import importlib.util
from pathlib import Path
from collections import defaultdict
from typing import List, Dict, Any, Optional, Tuple, Set

from raccoon.ide.core.analysis.step_analyzer import DSLStepAnalyzer
from raccoon.ide.core.naming import normalize_name
from raccoon.ide.schemas.mission_detail import ParsedMission, ParsedStep, StepArgument, ParsedComment


class MissionCodeGenerator:
    """Render ``ParsedMission`` payloads back into importable Python code."""

    _dynamic_import_cache: Optional[Dict[str, str]] = None

    def __init__(self):
        self.indent = "    "  # 4 spaces
        self.used_functions = set()  # Track functions used in the mission
        self.comments_before_step: Dict[str, List[ParsedComment]] = {}
        self.comments_after_step: Dict[str, List[ParsedComment]] = {}
        self.header_comments: List[ParsedComment] = []
        self.orphan_comments: List[ParsedComment] = []

    def generate_mission_code(self, parsed_mission: ParsedMission) -> str:
        """Generate complete mission code from parsed structure."""
        lines = []
        self.used_functions.clear()
        self._prepare_comments(parsed_mission.comments or [])

        # First pass: collect all used functions
        self._collect_used_functions(parsed_mission.steps)

        # Generate imports
        import_lines = self._generate_imports()
        lines.extend(import_lines)
        lines.append("")  # Empty line after imports

        # Add class definition
        lines.append(f"class {parsed_mission.name}(Mission):")

        # Add sequence method
        lines.append(f"{self.indent}def sequence(self) -> Sequential:")

        header_indent = self.indent * 2

        # Check if we need to wrap in seq([]) or if the steps are already properly structured
        if self._needs_outer_seq(parsed_mission.steps):
            step_lines = self._generate_steps(parsed_mission.steps, indent_level=3, path_prefix=())
            orphan_comments = self._gather_remaining_comments()
            self.orphan_comments = orphan_comments
            header_comments = list(self.header_comments)
            header_comments.extend(orphan_comments)
            header_lines = self._format_comment_block(header_comments, header_indent)
            if header_lines:
                lines.extend(header_lines)
            lines.append(f"{self.indent * 2}return seq([")
            lines.extend(step_lines)
            lines.append(f"{self.indent * 2}])")
        else:
            # Steps are already structured, just return them directly
            step_lines = self._generate_steps(parsed_mission.steps, indent_level=2, return_prefix="return ",
                                              path_prefix=())
            orphan_comments = self._gather_remaining_comments()
            self.orphan_comments = orphan_comments
            header_comments = list(self.header_comments)
            header_comments.extend(orphan_comments)
            header_lines = self._format_comment_block(header_comments, header_indent)
            if header_lines:
                lines.extend(header_lines)
            lines.extend(step_lines)
        lines.append("")  # Empty line at end

        return "\n".join(lines)

    def _needs_outer_seq(self, steps: List[ParsedStep]) -> bool:
        """Determine if steps need to be wrapped in an outer seq([])."""
        # If there's only one step and it's a seq, we don't need outer wrapping
        if len(steps) == 1 and steps[0].function_name == "seq":
            return False
        # If there are multiple steps or no seq steps, we need outer wrapping
        return True

    def _collect_used_functions(self, steps: List[ParsedStep]) -> None:
        """Collect all function names used in the mission."""
        for step in steps:
            self.used_functions.add(step.function_name)
            if step.children:
                self._collect_used_functions(step.children)

    def _prepare_comments(self, comments: List[ParsedComment]) -> None:
        """Organize comments for placement during code generation."""
        self.comments_before_step = defaultdict(list)
        self.comments_after_step = defaultdict(list)
        self.header_comments = []
        self.orphan_comments = []

        for comment in comments:
            if comment is None:
                continue
            after_path = getattr(comment, "after_path", None)
            before_path = getattr(comment, "before_path", None)

            if after_path:
                self.comments_before_step[after_path].append(comment)
            elif before_path:
                self.comments_after_step[before_path].append(comment)
            else:
                self.header_comments.append(comment)

    def _format_comment_lines(self, comment: ParsedComment, indent: str) -> List[str]:
        """Convert a comment into properly indented Python comment lines."""
        text = getattr(comment, "text", "") or ""
        lines: List[str] = []

        if text == "":
            return [f"{indent}#"]

        for raw_line in str(text).splitlines():
            line = raw_line.rstrip()
            if line:
                lines.append(f"{indent}# {line}")
            else:
                lines.append(f"{indent}#")

        return lines or [f"{indent}#"]

    def _format_comment_block(self, comments: List[ParsedComment], indent: str) -> List[str]:
        """Format a sequence of comments with consistent indentation."""
        lines: List[str] = []
        for comment in comments:
            lines.extend(self._format_comment_lines(comment, indent))
        return lines

    def _consume_comments_before(self, path: str, indent: str) -> List[str]:
        """Retrieve and remove comments that should appear before the given path."""
        comments = self.comments_before_step.pop(path, [])
        return self._format_comment_block(comments, indent)

    def _consume_comments_after(self, path: str, indent: str) -> List[str]:
        """Retrieve and remove comments that should appear after the given path."""
        comments = self.comments_after_step.pop(path, [])
        return self._format_comment_block(comments, indent)

    def _path_to_str(self, path: Tuple[int, ...]) -> str:
        """Convert a tuple path to dotted string representation."""
        return ".".join(str(segment) for segment in path)

    def _gather_remaining_comments(self) -> List[ParsedComment]:
        """Collect comments whose anchors were not matched."""
        remaining: List[ParsedComment] = []
        for comment_list in self.comments_before_step.values():
            remaining.extend(comment_list)
        for comment_list in self.comments_after_step.values():
            remaining.extend(comment_list)
        self.comments_before_step.clear()
        self.comments_after_step.clear()
        return remaining

    def _generate_imports(self) -> List[str]:
        """Generate import statements based on used functions."""
        imports = []

        # Always needed imports
        imports.append("from libstp.mission.api import Mission")
        imports.append("from libstp.step.sequential import Sequential, seq")

        added_imports = set(imports)
        for func_name in sorted(self.used_functions):
            import_stmt = self._resolve_import_statement(func_name)
            if import_stmt and import_stmt not in added_imports:
                imports.append(import_stmt)
                added_imports.add(import_stmt)

        return imports

    def _resolve_import_statement(self, func_name: str) -> Optional[str]:
        import_path = self._get_step_import_path(func_name)
        if not import_path:
            return None
        module_path, _, symbol = import_path.rpartition(".")
        if not module_path or not symbol:
            return None
        return f"from {module_path} import {symbol}"

    def _get_step_import_path(self, func_name: str) -> Optional[str]:
        dynamic_map = self._get_dynamic_step_import_map()
        if func_name in dynamic_map:
            return dynamic_map[func_name]
        return self._static_step_import_paths().get(func_name)

    @classmethod
    def _get_dynamic_step_import_map(cls) -> Dict[str, str]:
        if cls._dynamic_import_cache is None:
            cls._dynamic_import_cache = cls._discover_dynamic_step_import_map()
        return cls._dynamic_import_cache

    @classmethod
    def _discover_dynamic_step_import_map(cls) -> Dict[str, str]:
        mapping: Dict[str, str] = {}
        for root_path, step_dir in cls._libstp_step_directories():
            analyzer = DSLStepAnalyzer(root_path)
            for file_path in step_dir.rglob("*.py"):
                analyzer._analyze_file(file_path)
            for step in analyzer.discovered_steps:
                if step.name not in mapping:
                    mapping[step.name] = step.import_path
        return mapping

    @staticmethod
    def _libstp_step_directories() -> List[Tuple[Path, Path]]:
        entries: Set[Tuple[Path, Path]] = set()
        workspace = Path.cwd()

        direct_step_dir = workspace / "libstp" / "step"
        if direct_step_dir.exists():
            entries.add((workspace.resolve(), direct_step_dir.resolve()))

        try:
            for child in workspace.iterdir():
                if not child.is_dir():
                    continue
                step_dir = child / "libstp" / "step"
                if step_dir.exists():
                    entries.add((child.resolve(), step_dir.resolve()))
        except PermissionError:
            pass

        spec = importlib.util.find_spec("libstp")
        if spec and spec.submodule_search_locations:
            for location in spec.submodule_search_locations:
                location_path = Path(location)
                step_dir = location_path / "step"
                if step_dir.exists():
                    entries.add((location_path.parent.resolve(), step_dir.resolve()))

        return sorted(entries, key=lambda pair: str(pair[1]))

    @staticmethod
    def _static_step_import_paths() -> Dict[str, str]:
        return {}

    def _generate_steps(
            self,
            steps: List[ParsedStep],
            indent_level: int = 0,
            return_prefix: str = "",
            path_prefix: Tuple[int, ...] = (),
    ) -> List[str]:
        """Generate code for a list of steps."""
        lines: List[str] = []
        indent = self.indent * indent_level
        total = len(steps)

        for index, step in enumerate(steps, start=1):
            current_path = path_prefix + (index,)
            path_str = self._path_to_str(current_path)

            lines.extend(self._consume_comments_before(path_str, indent))

            step_code = self._generate_single_step(step, indent_level, current_path)

            has_breakpoint_children = step.function_name == "breakpoint" and bool(step.children)
            needs_trailing_comma = (index < total and not return_prefix) or has_breakpoint_children
            if needs_trailing_comma and not step_code.rstrip().endswith(","):
                step_code += ","

            prefix = return_prefix if index == 1 and return_prefix else ""
            lines.append(f"{indent}{prefix}{step_code}")

            lines.extend(self._consume_comments_after(path_str, indent))

            if has_breakpoint_children:
                child_lines = self._generate_steps(step.children or [], indent_level, path_prefix=current_path)
                if child_lines:
                    if index < total:
                        last_idx = len(child_lines) - 1
                        if not child_lines[last_idx].rstrip().endswith(","):
                            child_lines[last_idx] = f"{child_lines[last_idx]},"
                    lines.extend(child_lines)

        return lines

    def _generate_single_step(self, step: ParsedStep, indent_level: int = 0, path: Tuple[int, ...] = ()) -> str:
        """Generate code for a single step."""
        func_name = step.function_name

        # Handle special container steps
        if step.children:
            if func_name == "parallel":
                return self._generate_parallel_step(step, indent_level, path)
            elif func_name == "seq":
                return self._generate_seq_step(step, indent_level, path)

        # Handle regular steps
        args = self._generate_arguments(step.arguments)
        return f"{func_name}({args})"

    def _generate_parallel_step(self, step: ParsedStep, indent_level: int, path: Tuple[int, ...]) -> str:
        """Generate code for parallel step with children."""
        if not step.children:
            return f"{step.function_name}()"

        lines = [f"{step.function_name}("]

        child_indent = self.indent * (indent_level + 1)
        total = len(step.children)
        for index, child in enumerate(step.children, start=1):
            child_path = path + (index,)
            path_str = self._path_to_str(child_path)

            lines.extend(self._consume_comments_before(path_str, child_indent))

            child_code = self._generate_single_step(child, indent_level + 1, child_path)
            if index < total:
                child_code += ","
            lines.append(f"{child_indent}{child_code}")

            lines.extend(self._consume_comments_after(path_str, child_indent))

        lines.append(f"{self.indent * indent_level})")

        return "\n".join(lines)

    def _generate_seq_step(self, step: ParsedStep, indent_level: int, path: Tuple[int, ...]) -> str:
        """Generate code for sequential step with children."""
        if not step.children:
            return f"{step.function_name}([])"

        lines = [f"{step.function_name}(["]

        child_lines = self._generate_steps(step.children, indent_level + 1, path_prefix=path)
        lines.extend(child_lines)

        lines.append(f"{self.indent * indent_level}])")

        return "\n".join(lines)

    def _generate_arguments(self, arguments: List[StepArgument]) -> str:
        """Generate argument list for a function call."""
        if not arguments:
            return ""

        args: List[str] = []

        positional_args: List[StepArgument] = []
        keyword_args: List[StepArgument] = []
        for argument in arguments:
            if StepArgument.is_keyword_argument(argument):
                keyword_args.append(argument)
            else:
                positional_args.append(argument)

        # Add positional arguments first
        for arg in positional_args:
            args.append(self._format_argument_value(arg.value))

        # Add keyword arguments
        for arg in keyword_args:
            value = self._format_argument_value(arg.value)
            args.append(f"{arg.name}={value}")

        return ", ".join(args)

    def _format_argument_value(self, value: Any) -> str:
        """Format a single argument value."""
        if isinstance(value, str):
            # Check if it's a lambda or complex expression
            if value.startswith("lambda") or any(char in value for char in ["(", ")", ".", "_"]):
                return value  # Return as-is for complex expressions
            else:
                return f'"{value}"'  # Quote regular strings
        elif isinstance(value, bool):
            return "True" if value else "False"
        elif value is None:
            return "None"
        else:
            return str(value)

    def update_mission_file(self, mission_file_path: Path, parsed_mission: ParsedMission) -> bool:
        """Update an existing mission file with new parsed mission data."""
        try:
            # Generate the new code
            new_code = self.generate_mission_code(parsed_mission)

            # Write to file
            mission_file_path.write_text(new_code, encoding="utf-8")

            return True
        except Exception as e:
            print(f"Error updating mission file {mission_file_path}: {e}")
            return False

    def create_mission_file(self, mission_dir: Path, parsed_mission: ParsedMission) -> Optional[Path]:
        """Create a new mission file from parsed mission data."""
        try:
            # Create filename from mission name
            normalized = normalize_name(parsed_mission.name)
            snake_base = normalized.snake or "mission"
            filename = f"{snake_base}_mission.py"
            mission_file_path = mission_dir / filename

            # Generate the code
            code = self.generate_mission_code(parsed_mission)

            # Write to file
            mission_file_path.write_text(code, encoding="utf-8")

            return mission_file_path
        except Exception as e:
            print(f"Error creating mission file: {e}")
            return None


class MissionUpdater:
    """
    Service for updating mission files from JSON data.
    """

    def __init__(self):
        self.code_generator = MissionCodeGenerator()

    def update_mission_from_json(self, project_root: Path, mission_data: Dict[str, Any]) -> bool:
        """Update a mission file from JSON data."""
        try:
            # Convert dict to ParsedMission
            parsed_mission = ParsedMission(**mission_data)

            # Find the mission file
            missions_dir = project_root / "src" / "missions"
            if not missions_dir.exists():
                missions_dir.mkdir(parents=True, exist_ok=True)

            normalized = normalize_name(parsed_mission.name)
            snake_case = normalized.snake or "mission"
            canonical_name = f"{snake_case}_mission.py"
            canonical_path = missions_dir / canonical_name

            # Look for an existing mission file whose normalized stem matches
            mission_file: Optional[Path] = None
            if canonical_path.exists():
                mission_file = canonical_path
            else:
                for potential_file in sorted(missions_dir.glob("*.py")):
                    stem_normalized = normalize_name(potential_file.stem).snake
                    if stem_normalized == snake_case:
                        mission_file = potential_file
                        break

            if mission_file and mission_file != canonical_path:
                try:
                    mission_file.rename(canonical_path)
                    mission_file = canonical_path
                except OSError:
                    # If rename fails, continue writing to the located file
                    pass

            if mission_file:
                # Update existing file
                return self.code_generator.update_mission_file(mission_file, parsed_mission)

            # Create new file if none existed
            new_file = self.code_generator.create_mission_file(missions_dir, parsed_mission)
            return new_file is not None

        except Exception as e:
            print(f"Error updating mission from JSON: {e}")
            return False
