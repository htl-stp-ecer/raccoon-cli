"""Project source formatting — Black with the project's own settings.

``raccoon format`` formats the hand-written Python under ``src/`` with Black.
Black itself is tuned in two ways so that mission code keeps its shape:

* ``hug_brackets`` keeps ``seq([`` and ``])`` on the same line as the call, so the
  steps of a mission are not pushed one indentation level deeper.
* ``explode_calls`` names calls (default ``seq`` and ``parallel``) whose arguments
  are always written one per line, however short they are.
* ``keep_blank_lines`` keeps blank lines that separate groups of steps inside a
  bracket (Black normally removes them). Several blank lines become one.

The settings live in ``raccoon.formatting.yml`` at the project root. Formatting is
opt-in: a project without that file is not formatted. Options missing from the
file take the defaults of :class:`FormattingConfig`. With ``auto_format: true``
``raccoon run`` formats the project before it starts.
"""

from __future__ import annotations

import ast
import fnmatch
import io
import re
import tokenize
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Set, Tuple

import yaml

FORMATTING_CONFIG_FILE = "raccoon.formatting.yml"

DEFAULT_EXCLUDE = ("src/hardware/**",)
DEFAULT_EXPLODE_CALLS = ("seq", "parallel")

# Black normalises "#text" to "# text", so the marker is already in that form.
_BLANK_MARKER = "# __raccoon_blank__"
_BLANK_MARKER_RE = re.compile(r"^[ \t]*" + re.escape(_BLANK_MARKER) + r"[ \t]*$", re.MULTILINE)


class FormattingConfigError(ValueError):
    """``raccoon.formatting.yml`` is malformed."""


@dataclass(frozen=True)
class FormattingConfig:
    enabled: bool = True
    auto_format: bool = False
    line_length: int = 88
    skip_string_normalization: bool = False
    skip_magic_trailing_comma: bool = False
    hug_brackets: bool = True
    keep_blank_lines: bool = True
    explode_calls: Tuple[str, ...] = DEFAULT_EXPLODE_CALLS
    exclude: Tuple[str, ...] = DEFAULT_EXCLUDE


_BOOL_KEYS = (
    "enabled",
    "auto_format",
    "skip_string_normalization",
    "skip_magic_trailing_comma",
    "hug_brackets",
    "keep_blank_lines",
)
_KNOWN_KEYS = frozenset({*_BOOL_KEYS, "line_length", "explode_calls", "exclude"})


def parse_formatting_config(raw: object) -> FormattingConfig:
    """Build a config from the parsed YAML content, rejecting unknown keys and bad types."""
    if raw is None:
        return FormattingConfig()
    if not isinstance(raw, dict):
        raise FormattingConfigError(f"{FORMATTING_CONFIG_FILE} must contain a mapping of options")

    unknown = sorted(str(key) for key in raw if key not in _KNOWN_KEYS)
    if unknown:
        raise FormattingConfigError(
            f"Unknown option(s) in {FORMATTING_CONFIG_FILE}: {', '.join(unknown)}. "
            f"Valid options: {', '.join(sorted(_KNOWN_KEYS))}"
        )

    values: dict = {}
    for key in _BOOL_KEYS:
        if key in raw:
            if not isinstance(raw[key], bool):
                raise FormattingConfigError(f"{key} must be true or false, got {raw[key]!r}")
            values[key] = raw[key]

    if "line_length" in raw:
        length = raw["line_length"]
        if isinstance(length, bool) or not isinstance(length, int) or length < 20:
            raise FormattingConfigError(f"line_length must be an integer of at least 20, got {length!r}")
        values["line_length"] = length

    for key in ("exclude", "explode_calls"):
        if key in raw:
            entries = raw[key]
            if not isinstance(entries, list) or not all(isinstance(e, str) for e in entries):
                raise FormattingConfigError(f"{key} must be a list of strings")
            values[key] = tuple(entries)

    return FormattingConfig(**values)


def has_formatting_config(project_root: Path) -> bool:
    """Whether the project has a ``raccoon.formatting.yml``."""
    return (project_root / FORMATTING_CONFIG_FILE).is_file()


def load_formatting_config(project_root: Path) -> FormattingConfig:
    """Load ``raccoon.formatting.yml`` from the project root (defaults when absent)."""
    path = project_root / FORMATTING_CONFIG_FILE
    if not path.is_file():
        return FormattingConfig()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise FormattingConfigError(f"Invalid YAML in {FORMATTING_CONFIG_FILE}: {exc}") from exc
    return parse_formatting_config(raw)


# ---------------------------------------------------------------------------
# Blank lines inside brackets
# ---------------------------------------------------------------------------

def _protect_blank_lines(source: str) -> str:
    """Replace each blank line inside brackets by a marker comment.

    Black drops blank lines inside brackets but keeps comments, so the markers
    survive formatting and are turned back into blank lines afterwards. Runs of
    blank lines collapse into one marker; blank lines directly after an opening
    bracket or before a closing one are dropped.
    """
    lines = source.split("\n")

    depth_at_line: dict[int, int] = {}
    in_multiline_token: Set[int] = set()
    depth = 0
    last_line = 0
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.end[0] > tok.start[0] and tok.type in (tokenize.STRING, tokenize.FSTRING_MIDDLE):
            in_multiline_token.update(range(tok.start[0] + 1, tok.end[0] + 1))
        for line_no in range(last_line + 1, tok.start[0] + 1):
            depth_at_line.setdefault(line_no, depth)
        last_line = max(last_line, tok.start[0])
        if tok.type == tokenize.OP:
            if tok.string in "([{":
                depth += 1
            elif tok.string in ")]}":
                depth -= 1

    result: List[str] = []
    previous_code = ""
    previous_was_marker = False
    for line_no, line in enumerate(lines, start=1):
        is_blank = line.strip() == ""
        if is_blank and line_no not in in_multiline_token and depth_at_line.get(line_no, 0) > 0:
            next_code = next((candidate for candidate in lines[line_no:] if candidate.strip()), "")
            after_opening = previous_code.rstrip().endswith(("(", "[", "{"))
            before_closing = next_code.lstrip().startswith((")", "]", "}"))
            if not (previous_was_marker or after_opening or before_closing):
                result.append(_BLANK_MARKER)
                previous_was_marker = True
            continue
        result.append(line)
        if not is_blank:
            previous_code = line
            previous_was_marker = False
    return "\n".join(result)


# ---------------------------------------------------------------------------
# Calls whose arguments are always one per line
# ---------------------------------------------------------------------------

def _call_name(func: ast.expr) -> Optional[str]:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _add_trailing_commas(source: str, names: Tuple[str, ...]) -> str:
    """Put a trailing comma into every call of ``names`` that lacks one.

    Black treats a trailing comma as "keep one item per line", so the arguments
    of these calls stay exploded however short they are. For ``seq([...])`` the
    comma goes after the last step in the list, so the list is exploded while
    ``seq([`` and ``])`` stay hugged.
    """
    if not names:
        return source
    tree = ast.parse(source)
    lines = source.split("\n")

    # Significant tokens (no comments / newlines) with character positions.
    significant: List[tokenize.TokenInfo] = [
        tok
        for tok in tokenize.generate_tokens(io.StringIO(source).readline)
        if tok.type not in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT)
    ]
    index_by_end = {tok.end: i for i, tok in enumerate(significant)}

    def char_col(line_no: int, byte_col: int) -> int:
        return len(lines[line_no - 1].encode("utf-8")[:byte_col].decode("utf-8", errors="ignore"))

    insert_at: Set[Tuple[int, int]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node.func) not in names:
            continue
        arguments = [*node.args, *node.keywords]
        if not arguments:
            continue
        if len(arguments) == 1 and isinstance(arguments[0], ast.GeneratorExp):
            continue  # f(x for x in y,) is a syntax error
        target = node
        if len(arguments) == 1 and isinstance(arguments[0], ast.List) and not node.keywords:
            target = arguments[0]
        closing = (target.end_lineno, char_col(target.end_lineno, target.end_col_offset))
        position = index_by_end.get(closing)
        if position is None or position == 0:
            continue
        last = significant[position - 1]
        if last.type == tokenize.OP and last.string in (",", "(", "[", "{"):
            continue  # already has a comma, or the brackets are empty (seq([]))
        insert_at.add(last.end)

    for line_no, col in sorted(insert_at, reverse=True):
        line = lines[line_no - 1]
        lines[line_no - 1] = line[:col] + "," + line[col:]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _black_mode(config: FormattingConfig):
    import black

    features: Set = set()
    if config.hug_brackets:
        hug = getattr(black.Preview, "hug_parens_with_braces_and_square_brackets", None)
        if hug is not None:
            features.add(hug)
    return black.Mode(
        line_length=config.line_length,
        string_normalization=not config.skip_string_normalization,
        magic_trailing_comma=not config.skip_magic_trailing_comma,
        enabled_features=features,
    )


def format_source(source: str, config: FormattingConfig) -> str:
    """Return ``source`` formatted according to ``config``.

    Raises ``SyntaxError``-like errors from Black for invalid code, and
    ``ValueError`` if formatting would change what the code does.
    """
    import black

    to_format = _add_trailing_commas(source, config.explode_calls)
    if config.keep_blank_lines:
        to_format = _protect_blank_lines(to_format)
    formatted = black.format_str(to_format, mode=_black_mode(config))
    if config.keep_blank_lines:
        formatted = _BLANK_MARKER_RE.sub("", formatted)

    if ast.dump(ast.parse(source)) != ast.dump(ast.parse(formatted)):
        raise ValueError("formatting would change the meaning of the code; file left untouched")
    return formatted


@dataclass
class FormatResult:
    config: FormattingConfig
    changed: List[Path] = field(default_factory=list)
    unchanged: int = 0
    errors: List[Tuple[Path, str]] = field(default_factory=list)
    config_missing: bool = False

    @property
    def disabled(self) -> bool:
        return not self.config.enabled


def _is_excluded(relative: str, patterns: Tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(relative, pattern) for pattern in patterns)


def find_source_files(project_root: Path, config: FormattingConfig) -> List[Path]:
    """Python files under ``src/`` that are not excluded."""
    src_root = project_root / "src"
    if not src_root.is_dir():
        return []
    files = []
    for path in sorted(src_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        if _is_excluded(path.relative_to(project_root).as_posix(), config.exclude):
            continue
        files.append(path)
    return files


def format_project(
    project_root: Path,
    *,
    check: bool = False,
    config: Optional[FormattingConfig] = None,
) -> FormatResult:
    """Format the project's source files; with ``check`` nothing is written.

    Without ``raccoon.formatting.yml`` (and no explicit ``config``) nothing is
    formatted and ``config_missing`` is set. Paths in the result are relative to
    ``project_root``.
    """
    if config is None:
        if not has_formatting_config(project_root):
            return FormatResult(config=FormattingConfig(enabled=False), config_missing=True)
        config = load_formatting_config(project_root)
    result = FormatResult(config=config)
    if not config.enabled:
        return result

    for path in find_source_files(project_root, config):
        relative = path.relative_to(project_root)
        try:
            original = path.read_bytes().decode("utf-8")
            crlf = "\r\n" in original
            formatted = format_source(original.replace("\r\n", "\n"), config)
            if crlf:
                formatted = formatted.replace("\n", "\r\n")
        except Exception as exc:  # Black raises its own InvalidInput, tokenize raises TokenError
            result.errors.append((relative, str(exc)))
            continue

        if formatted == original:
            result.unchanged += 1
            continue
        result.changed.append(relative)
        if not check:
            path.write_bytes(formatted.encode("utf-8"))
    return result
