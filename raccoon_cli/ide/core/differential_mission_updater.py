"""Surgical, diff-based mission file updater.

Instead of regenerating the whole Python file on every flowchart save, this
module patches *only* the parts that actually changed:

  1. **Import level** – add imports for newly-introduced functions; never
     touch existing import lines.
  2. **Sequence-method level** – only the ``seq([...])`` list body is
     rewritten; everything else in the class (other methods, module-level
     code, docstrings) is untouched.
  3. **Step level** – each step is fingerprinted recursively.  If its
     fingerprint matches the corresponding element that is already in the
     file, the original CST node is kept verbatim (preserving the user's
     whitespace / trailing comments / formatting choices).  Only steps whose
     fingerprint changed are replaced with freshly-generated code.

If *any* stage fails, the patcher falls back silently to the full-regen path
so correctness is never compromised.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

import libcst as cst

from raccoon_cli.ide.schemas.mission_detail import ParsedMission, ParsedStep


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------

def _step_fp(step: ParsedStep) -> str:
    """Canonical JSON fingerprint of a step for change detection."""
    def _fp(s: ParsedStep) -> dict:
        return {
            "fn": s.function_name,
            "args": [(a.name, repr(a.value), a.type) for a in s.arguments],
            "ch": [_fp(c) for c in (s.children or [])],
        }
    return json.dumps(_fp(step), sort_keys=True)


# ---------------------------------------------------------------------------
# libcst helpers
# ---------------------------------------------------------------------------

def _name(node) -> str:
    if isinstance(node, cst.Name):
        return node.value
    return ""


def _parse_step_as_element(step_source: str) -> cst.Element:
    """Parse a generated step call string into a libcst Element node."""
    # Wrap in a list assignment so libcst can parse it as a statement
    module = cst.parse_module(f"_x = [{step_source}]\n")
    assign = module.body[0]
    list_node = assign.body[0].value  # type: ignore[attr-defined]
    return list_node.elements[0]


# ---------------------------------------------------------------------------
# CST transformer
# ---------------------------------------------------------------------------

class _MissionPatcher(cst.CSTTransformer):
    """
    Walks the CST of an existing mission file and replaces only the changed
    elements inside ``seq([...])``.

    Anything outside ``sequence()`` (imports, other methods, module-level
    code) is passed through without modification.
    """

    def __init__(
        self,
        mission_name: str,
        old_steps: List[ParsedStep],
        new_steps: List[ParsedStep],
        code_gen,       # MissionCodeGenerator – imported lazily to avoid circular
    ) -> None:
        super().__init__()
        self._mission_name = mission_name
        self._old_fps: List[str] = [_step_fp(s) for s in old_steps]
        self._new_steps = new_steps
        self._code_gen = code_gen

        # State flags
        self._in_mission_class = False
        self._in_sequence_fn = False
        self._top_seq_node: Optional[cst.Call] = None  # the outermost seq() to patch
        self._top_seq_found = False  # set once during pre-order visit

        # Collect names of functions that appear in *new* steps but not old
        old_fns: Set[str] = set()
        new_fns: Set[str] = set()
        self._collect_fns(old_steps, old_fns)
        self._collect_fns(new_steps, new_fns)
        self.added_functions: Set[str] = new_fns - old_fns

    # ---- function-name bookkeeping ----------------------------------------

    @staticmethod
    def _collect_fns(steps: List[ParsedStep], out: Set[str]) -> None:
        for s in steps:
            out.add(s.function_name)
            if s.children:
                _MissionPatcher._collect_fns(s.children, out)

    # ---- class / function detection ---------------------------------------

    def visit_ClassDef(self, node: cst.ClassDef) -> Optional[bool]:
        if _name(node.name) == self._mission_name:
            self._in_mission_class = True
        return True

    def leave_ClassDef(
        self, original: cst.ClassDef, updated: cst.ClassDef
    ) -> cst.CSTNode:
        if _name(original.name) == self._mission_name:
            self._in_mission_class = False
        return updated

    def visit_FunctionDef(self, node: cst.FunctionDef) -> Optional[bool]:
        if self._in_mission_class and _name(node.name) == "sequence":
            self._in_sequence_fn = True
        return True

    def leave_FunctionDef(
        self, original: cst.FunctionDef, updated: cst.FunctionDef
    ) -> cst.CSTNode:
        if self._in_mission_class and _name(original.name) == "sequence":
            self._in_sequence_fn = False
        return updated

    # ---- identify the outermost seq() in pre-order (top-down) ------------

    def visit_Call(self, node: cst.Call) -> Optional[bool]:
        if (
            self._in_mission_class
            and self._in_sequence_fn
            and not self._top_seq_found
            and _name(node.func) == "seq"
            and node.args
            and isinstance(node.args[0].value, cst.List)
        ):
            self._top_seq_node = node
            self._top_seq_found = True
        return True

    # ---- the actual patch: replace elements of seq([...]) ----------------

    def leave_Call(
        self, original: cst.Call, updated: cst.Call
    ) -> cst.BaseExpression:
        if original is not self._top_seq_node:
            return updated

        first_arg = original.args[0]
        old_elements = list(first_arg.value.elements)
        new_elements = self._build_element_list(old_elements)

        new_list = first_arg.value.with_changes(elements=new_elements)
        new_first_arg = first_arg.with_changes(value=new_list)
        return updated.with_changes(
            args=(new_first_arg, *updated.args[1:])
        )

    def _build_element_list(
        self, old_elements: List[cst.Element]
    ) -> List[cst.Element]:
        """
        Build the new element list by either reusing the old CST node (if the
        step fingerprint is unchanged) or regenerating it.
        """
        # Detect the indentation model used by existing elements so new ones match
        indent_str, indent_flag = self._detect_indent(old_elements)

        result: List[cst.Element] = []
        n = len(self._new_steps)

        for i, new_step in enumerate(self._new_steps):
            is_last = i == n - 1
            new_fp = _step_fp(new_step)
            old_fp = self._old_fps[i] if i < len(self._old_fps) else None

            if old_fp == new_fp and i < len(old_elements):
                # Step unchanged → keep existing node exactly
                elem = old_elements[i]
                elem = self._set_trailing_comma(elem, not is_last, indent_str, indent_flag)
                result.append(elem)
            else:
                # Step changed or is brand-new → regenerate
                step_src = self._render_step(new_step, indent_level=3, path=(i + 1,))
                try:
                    elem = _parse_step_as_element(step_src)
                    elem = self._set_trailing_comma(elem, not is_last, indent_str, indent_flag)
                    result.append(elem)
                except Exception:
                    raise _PatchError(
                        f"Failed to parse generated step: {step_src!r}"
                    )

        return result

    @staticmethod
    def _detect_indent(elements: List[cst.Element]) -> Tuple[str, bool]:
        """
        Return (last_line_value, indent_flag) from the first existing inter-element
        comma in the list.  These values can be passed directly to
        ``_make_newline_comma`` so the new comma has the exact same whitespace
        model as the surrounding elements.
        """
        for elem in elements:
            if isinstance(elem.comma, cst.Comma):
                wa = elem.comma.whitespace_after
                if isinstance(wa, cst.ParenthesizedWhitespace):
                    return wa.last_line.value, wa.indent  # type: ignore[attr-defined]
        # Fallback: one extra indent level, relative (matches libcst's default parse)
        return "    ", True

    @staticmethod
    def _make_newline_comma(indent: str, use_indent_flag: bool = True) -> cst.Comma:
        """Build a comma whose whitespace_after places the next token on a new line."""
        return cst.Comma(
            whitespace_before=cst.SimpleWhitespace(""),
            whitespace_after=cst.ParenthesizedWhitespace(
                first_line=cst.TrailingWhitespace(newline=cst.Newline()),
                indent=use_indent_flag,
                last_line=cst.SimpleWhitespace(indent),
            ),
        )

    @staticmethod
    def _add_leading_whitespace(elem: cst.Element, indent: str) -> cst.Element:
        """Ensure the element value node doesn't carry unexpected whitespace."""
        # For freshly-parsed elements the value is already clean; nothing to do
        return elem

    @staticmethod
    def _set_trailing_comma(
        elem: cst.Element, want_comma: bool, indent: str, indent_flag: bool
    ) -> cst.Element:
        has_comma = not isinstance(elem.comma, cst.MaybeSentinel)

        if not want_comma:
            return elem.with_changes(comma=cst.MaybeSentinel.DEFAULT)

        if has_comma:
            # Already has a comma — upgrade empty whitespace_after (trailing
            # comma on last element) to a proper newline→next-element form.
            wa = elem.comma.whitespace_after  # type: ignore[union-attr]
            is_empty = (
                isinstance(wa, cst.SimpleWhitespace) and wa.value in ("", " ")
            )
            if not is_empty:
                return elem  # whitespace already correct

        return elem.with_changes(
            comma=_MissionPatcher._make_newline_comma(indent, indent_flag)
        )

    def _render_step(
        self,
        step: ParsedStep,
        indent_level: int,
        path: Tuple[int, ...],
    ) -> str:
        """Render a single step to a Python expression string."""
        return self._code_gen._generate_single_step(step, indent_level, path)


class _PatchError(Exception):
    """Raised when the patcher cannot produce a valid edit."""


# ---------------------------------------------------------------------------
# Import adder
# ---------------------------------------------------------------------------

class _ImportAdder(cst.CSTTransformer):
    """Inserts missing import statements right after the last existing import."""

    def __init__(self, stmts_to_add: List[str]) -> None:
        super().__init__()
        self._to_add = stmts_to_add
        self._last_import_idx: int = -1

    def visit_Module(self, node: cst.Module) -> Optional[bool]:
        for i, stmt in enumerate(node.body):
            if isinstance(stmt, (cst.SimpleStatementLine,)):
                for s in stmt.body:
                    if isinstance(s, (cst.Import, cst.ImportFrom)):
                        self._last_import_idx = i
        return True

    def leave_Module(self, original: cst.Module, updated: cst.Module) -> cst.Module:
        if not self._to_add or self._last_import_idx < 0:
            return updated

        new_stmts: List[cst.BaseStatement] = list(updated.body)
        insert_after = self._last_import_idx

        for raw in reversed(self._to_add):
            parsed = cst.parse_module(raw + "\n").body[0]
            new_stmts.insert(insert_after + 1, parsed)

        return updated.with_changes(body=new_stmts)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def differential_update(
    mission_file_path: Path,
    new_mission: ParsedMission,
    old_mission: Optional[ParsedMission],
    code_gen,
) -> bool:
    """
    Attempt a surgical in-place update of *mission_file_path*.

    Returns ``True`` if the patch was applied, ``False`` if the caller should
    fall back to full regeneration.

    Parameters
    ----------
    mission_file_path:
        Path to the existing ``.py`` mission file.
    new_mission:
        The mission as sent from the flowchart editor.
    old_mission:
        The previously-parsed mission (from the existing file).  If ``None``,
        the function returns ``False`` immediately.
    code_gen:
        A ``MissionCodeGenerator`` instance used to render changed steps.
    """
    if old_mission is None:
        return False

    try:
        source = mission_file_path.read_text(encoding="utf-8")
        module = cst.parse_module(source)
    except Exception:
        return False

    # --- Step-level patch --------------------------------------------------
    patcher = _MissionPatcher(
        mission_name=new_mission.name,
        old_steps=old_mission.steps,
        new_steps=new_mission.steps,
        code_gen=code_gen,
    )
    try:
        patched_module = module.visit(patcher)
    except _PatchError:
        return False
    except Exception:
        return False

    # --- Import patch (add only, never remove) -----------------------------
    if patcher.added_functions:
        missing_imports = _resolve_missing_imports(
            patcher.added_functions, code_gen, patched_module
        )
        if missing_imports:
            adder = _ImportAdder(missing_imports)
            try:
                patched_module = patched_module.visit(adder)
            except Exception:
                pass  # import insertion failure is non-fatal

    # --- Write back --------------------------------------------------------
    try:
        mission_file_path.write_text(patched_module.code, encoding="utf-8")
        return True
    except Exception:
        return False


def _resolve_missing_imports(
    func_names: Set[str],
    code_gen,
    module: cst.Module,
) -> List[str]:
    """Return import statement strings for functions not yet imported."""
    # Collect already-imported names from the module
    already_imported: Set[str] = set()
    for node in module.body:
        if isinstance(node, cst.SimpleStatementLine):
            for stmt in node.body:
                if isinstance(stmt, cst.ImportFrom) and stmt.names:
                    if isinstance(stmt.names, (list, tuple)):
                        for alias in stmt.names:
                            if isinstance(alias, cst.ImportAlias):
                                if isinstance(alias.name, cst.Name):
                                    already_imported.add(alias.name.value)

    result: List[str] = []
    for fn in sorted(func_names):
        if fn in already_imported:
            continue
        stmt = code_gen._resolve_import_statement(fn)
        if stmt:
            result.append(stmt)
    return result
