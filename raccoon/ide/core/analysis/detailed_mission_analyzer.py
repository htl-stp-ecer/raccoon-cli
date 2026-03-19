"""Parse mission source files into rich mission-detail schema objects.

This module uses ``libcst`` so the IDE can recover mission structure, step
arguments, and source positions without executing user code.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Any, Dict
import libcst as cst
from libcst.metadata import PositionProvider

from raccoon.ide.schemas.mission_detail import ParsedMission, ParsedStep, StepArgument, Vector2D


class DetailedMissionExtractor(cst.CSTVisitor):
    """
    Extracts detailed mission information including steps and their arguments.
    """
    METADATA_DEPENDENCIES = (PositionProvider,)

    def __init__(self) -> None:
        self.missions: Dict[str, ParsedMission] = {}
        self.current_mission_name: Optional[str] = None

    def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
        """Skip import processing - imports will be auto-resolved."""
        pass

    def visit_ClassDef(self, node: cst.ClassDef) -> None:
        """Visit class definitions to find Mission classes."""
        if isinstance(node.name, cst.Name):
            class_name = node.name.value

            # Check if this class extends Mission
            is_mission_class = False
            if node.bases:
                for base in node.bases:
                    if isinstance(base.value, cst.Name) and base.value.value == "Mission":
                        is_mission_class = True
                        break

            if is_mission_class:
                self.current_mission_name = class_name
                self.missions[class_name] = ParsedMission(
                    name=class_name,
                    is_setup=False,
                    is_shutdown=False,
                    order=0,
                    steps=[]
                )

    def leave_ClassDef(self, node: cst.ClassDef) -> None:
        """Leave class definition."""
        self.current_mission_name = None

    def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
        """Visit function definitions within Mission classes."""
        if (self.current_mission_name and
            isinstance(node.name, cst.Name) and
            node.name.value == "sequence"):

            # Find the return statement and parse it
            for stmt in node.body.body:
                if isinstance(stmt, cst.SimpleStatementLine):
                    for simple_stmt in stmt.body:
                        if isinstance(simple_stmt, cst.Return) and simple_stmt.value:
                            steps = self._parse_return_value(simple_stmt.value, stmt)
                            if self.current_mission_name:
                                self.missions[self.current_mission_name].steps = steps

    def _parse_return_value(self, node: cst.BaseExpression, statement: cst.SimpleStatementLine) -> List[ParsedStep]:
        """Parse the return value of the sequence method."""
        if isinstance(node, cst.Call):
            return self._parse_call_expression(node, statement)
        return []

    def _parse_call_expression(self, node: cst.Call, context_stmt: cst.SimpleStatementLine) -> List[ParsedStep]:
        """Parse a call expression to extract step information."""
        if isinstance(node.func, cst.Name):
            func_name = node.func.value

            if func_name == "seq" and node.args:
                # Handle seq([...]) - extract the list argument
                first_arg = node.args[0]
                if isinstance(first_arg.value, cst.List):
                    steps = self._parse_step_list(first_arg.value.elements, context_stmt)
                    # If there's only one step and it's a seq, flatten it
                    if len(steps) == 1 and steps[0].function_name == "seq" and steps[0].children:
                        return steps[0].children
                    return steps

        return []

    def _parse_step_list(self, elements: List[cst.Element], context_stmt: cst.SimpleStatementLine) -> List[ParsedStep]:
        """Parse a list of steps."""
        steps = []

        for element in elements:
            if isinstance(element.value, cst.Call):
                step = self._parse_single_step(element.value, context_stmt)
                if step:
                    steps.append(step)

        return steps

    def _parse_single_step(self, node: cst.Call, context_stmt: cst.SimpleStatementLine) -> Optional[ParsedStep]:
        """Parse a single step call."""
        try:
            # Get function name
            func_name = ""
            if isinstance(node.func, cst.Name):
                func_name = node.func.value
            elif isinstance(node.func, cst.Attribute):
                func_name = cst.Module([]).code_for_node(node.func).strip()

            # Handle special container steps
            children = None
            arguments = []

            if func_name == "parallel":
                # For parallel steps, only use children, no arguments
                children = []
                for arg in node.args:
                    if isinstance(arg.value, cst.Call):
                        child_step = self._parse_single_step(arg.value, context_stmt)
                        if child_step:
                            children.append(child_step)
            elif func_name == "seq":
                # For seq steps, parse the list argument as children
                children = []
                if node.args:
                    first_arg = node.args[0]
                    if isinstance(first_arg.value, cst.List):
                        children = self._parse_step_list(first_arg.value.elements, context_stmt)
            else:
                # For regular steps, parse arguments
                for i, arg in enumerate(node.args):
                    arg_value = self._extract_argument_value(arg.value)

                    if arg.keyword:  # Keyword argument
                        arg_name = arg.keyword.value if isinstance(arg.keyword, cst.Name) else str(arg.keyword)
                        arguments.append(StepArgument(
                            name=arg_name,
                            value=arg_value,
                            type="keyword"
                        ))
                    else:  # Positional argument
                        arguments.append(StepArgument(
                            name=None,
                            value=arg_value,
                            type="positional"
                        ))

            return ParsedStep(
                step_type=func_name,
                function_name=func_name,
                arguments=arguments,
                children=children,
                position=self._extract_position(node)
            )

        except Exception as e:
            print(f"Error parsing step: {e}")
            return None

    def _extract_argument_value(self, node: cst.BaseExpression) -> Any:
        """Extract the value from an argument node."""
        try:
            if isinstance(node, cst.Integer):
                return int(node.value)
            elif isinstance(node, cst.Float):
                return float(node.value)
            elif isinstance(node, cst.SimpleString):
                # Remove quotes and handle escape sequences
                value = node.value
                if value.startswith('"') and value.endswith('"'):
                    return value[1:-1]
                elif value.startswith("'") and value.endswith("'"):
                    return value[1:-1]
                return value
            elif isinstance(node, cst.Name):
                # Handle True, False, None
                if node.value == "True":
                    return True
                elif node.value == "False":
                    return False
                elif node.value == "None":
                    return None
                else:
                    return node.value  # Variable name as string
            elif isinstance(node, cst.Lambda):
                # For lambda functions, return a string representation
                return cst.Module([]).code_for_node(node).strip()
            else:
                # For complex expressions, return string representation
                return cst.Module([]).code_for_node(node).strip()
        except Exception:
            return str(node)

    def _extract_position(self, node: cst.CSTNode) -> Vector2D:
        """Retrieve the start position (column, line) of a node."""
        try:
            code_range = self.get_metadata(PositionProvider, node)
            return Vector2D(x=code_range.start.column, y=code_range.start.line)
        except KeyError:
            return Vector2D(x=0, y=0)


class DetailedMissionAnalyzer:
    """
    Analyzes mission files to extract detailed step information.

    Results are cached by (file_path, mtime) so repeated lookups for the same
    unchanged file skip the expensive libcst parse.
    """

    def __init__(self) -> None:
        # Cache: file_path -> (mtime_ns, {mission_name: ParsedMission})
        self._cache: Dict[Path, tuple[int, Dict[str, ParsedMission]]] = {}

    def analyze_mission_file(self, mission_file_path: Path) -> Optional[ParsedMission]:
        """Analyze a single mission file (cached by mtime)."""
        missions = self._analyze_mission_file_all(mission_file_path)
        if missions:
            return next(iter(missions.values()))
        return None

    def _analyze_mission_file_all(self, mission_file_path: Path) -> Dict[str, ParsedMission]:
        """Parse all missions in a file, returning them keyed by class name.

        Uses an mtime-based cache to skip re-parsing unchanged files.
        """
        if not mission_file_path.exists():
            return {}

        try:
            mtime_ns = mission_file_path.stat().st_mtime_ns
        except OSError:
            return {}

        cached = self._cache.get(mission_file_path)
        if cached is not None and cached[0] == mtime_ns:
            return cached[1]

        try:
            code = mission_file_path.read_text(encoding="utf-8")
            module = cst.parse_module(code)
            wrapper = cst.metadata.MetadataWrapper(module)
            visitor = DetailedMissionExtractor()
            wrapper.visit(visitor)
            result = dict(visitor.missions)
        except Exception as e:
            print(f"Error analyzing mission file {mission_file_path}: {e}")
            result = {}

        self._cache[mission_file_path] = (mtime_ns, result)
        return result

    def analyze_mission_by_name(self, project_root: Path, mission_name: str) -> Optional[ParsedMission]:
        """Analyze a specific mission by name.

        Uses a naming-convention shortcut to try the expected file first,
        then falls back to scanning all files if needed.
        """
        missions_dir = project_root / "src" / "missions"

        if not missions_dir.exists():
            return None

        # Build candidate filenames from the mission class name.
        # Convention: class "M01DriveToConeMission" -> file "m01_drive_to_cone_mission.py"
        import re
        snake = re.sub(r'(?<!^)(?=[A-Z])', '_', mission_name).lower()
        candidates = [
            missions_dir / f"{snake}.py",
            missions_dir / f"{mission_name}.py",
            missions_dir / f"{mission_name.lower()}.py",
        ]
        # Try likely filenames first (avoids scanning all files)
        for candidate in candidates:
            if candidate.exists():
                missions = self._analyze_mission_file_all(candidate)
                if mission_name in missions:
                    return missions[mission_name]

        # Fallback: scan all files (cache will prevent re-parsing)
        for mission_file in missions_dir.glob("*.py"):
            if mission_file.name == "__init__.py":
                continue
            missions = self._analyze_mission_file_all(mission_file)
            if mission_name in missions:
                return missions[mission_name]

        return None
