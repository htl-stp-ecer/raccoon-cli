"""Detailed IDE mission schema models used for editing and simulation."""

from typing import List, Union, Optional, Any
from pydantic import BaseModel


class Vector2D(BaseModel):
    """2D point expressed in canvas coordinates."""

    x: float
    y: float


class Size2D(BaseModel):
    """Width and height pair used by visual editor layout objects."""

    width: float
    height: float


class StepArgument(BaseModel):
    """Serialized mission step argument captured by the IDE."""

    name: Optional[str] = None
    value: Union[str, int, float, bool, None]
    type: Optional[str] = None  # typically "positional" or "keyword"

    _KEYWORD_VALUES = {"keyword", "kw", "named", "named_argument"}
    _POSITIONAL_VALUES = {"positional", "pos", "position"}

    def _normalized_binding(self) -> str:
        raw_value = (self.type or "").strip().lower()
        if raw_value in self._KEYWORD_VALUES:
            return "keyword"
        if raw_value in self._POSITIONAL_VALUES:
            return "positional"
        return ""

    def binding(self) -> str:
        """Return the resolved binding type (keyword/positional)."""
        normalized = self._normalized_binding()
        if normalized:
            return normalized
        return "keyword" if self.name else "positional"

    def is_keyword(self) -> bool:
        return self.binding() == "keyword"

    def is_positional(self) -> bool:
        return self.binding() == "positional"

    @staticmethod
    def binding_for(argument: Any) -> str:
        if isinstance(argument, StepArgument):
            return argument.binding()

        raw_type = str(getattr(argument, "type", "") or "").strip().lower()
        if raw_type in StepArgument._KEYWORD_VALUES:
            return "keyword"
        if raw_type in StepArgument._POSITIONAL_VALUES:
            return "positional"

        name = getattr(argument, "name", None)
        return "keyword" if name else "positional"

    @staticmethod
    def is_keyword_argument(argument: Any) -> bool:
        return StepArgument.binding_for(argument) == "keyword"

    @staticmethod
    def is_positional_argument(argument: Any) -> bool:
        return StepArgument.binding_for(argument) == "positional"


class ParsedComment(BaseModel):
    """Canvas comment attached to a mission or a specific step path."""

    id: str
    text: str
    position: Optional[Vector2D] = None
    before_path: Optional[str] = None
    after_path: Optional[str] = None


class ParsedGroup(BaseModel):
    """Visual grouping metadata for a set of steps in the editor."""

    id: str
    title: str = "Group"
    position: Optional[Vector2D] = None
    size: Optional[Size2D] = None
    expanded_size: Optional[Size2D] = None
    collapsed: bool = False
    step_paths: List[str] = []


class ParsedStep(BaseModel):
    """Parsed mission step including layout data and optional child steps."""

    step_type: str  # e.g., "drive_forward", "custom_step", "parallel"
    function_name: str
    arguments: List[StepArgument]
    position: Vector2D
    # For container steps like parallel, seq - when children exist, arguments should be empty
    children: Optional[List['ParsedStep']] = None


class ParsedMission(BaseModel):
    """Full editable mission document returned by detailed mission parsing."""

    name: str
    is_setup: bool = False
    is_shutdown: bool = False
    order: int
    steps: List[ParsedStep]
    comments: List[ParsedComment] = []
    groups: List[ParsedGroup] = []
    #: Seconds available for setup.  Only meaningful when ``is_setup=True``.
    #: Emitted as a ``setup_time = <n>`` class attribute in generated code.
    setup_time: Optional[int] = None


# For Pydantic v2 compatibility
ParsedStep.model_rebuild()
ParsedMission.model_rebuild()
