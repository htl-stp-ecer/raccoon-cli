from pathlib import Path

from raccoon_cli.ide.core.analysis.step_analyzer import DSLStepAnalyzer


def _analyze_source(tmp_path: Path, source: str):
    file_path = tmp_path / "src" / "steps" / "sample_steps.py"
    file_path.parent.mkdir(parents=True)
    file_path.write_text(source, encoding="utf-8")

    analyzer = DSLStepAnalyzer(tmp_path)
    analyzer._analyze_file(file_path)
    return {step.name: step for step in analyzer.discovered_steps}


def test_keeps_explicit_annotations(tmp_path: Path):
    steps = _analyze_source(
        tmp_path,
        """
from raccoon import dsl

@dsl
def annotated_threshold(threshold: float = 0.7):
    return threshold
""".strip(),
    )

    assert steps["annotated_threshold"].arguments[0].type_name == "float"


def test_infers_scalar_types_from_literal_defaults(tmp_path: Path):
    steps = _analyze_source(
        tmp_path,
        """
from raccoon import dsl

@dsl
def inferred_defaults(
    threshold=0.7,
    retries=3,
    enabled=True,
    label="front",
    offset=-2.5,
):
    return threshold, retries, enabled, label, offset
""".strip(),
    )

    args = {arg.name: arg for arg in steps["inferred_defaults"].arguments}

    assert args["threshold"].type_name == "float"
    assert args["retries"].type_name == "int"
    assert args["enabled"].type_name == "bool"
    assert args["label"].type_name == "str"
    assert args["offset"].type_name == "float"


def test_keeps_any_for_non_literal_or_ambiguous_defaults(tmp_path: Path):
    steps = _analyze_source(
        tmp_path,
        """
from raccoon import dsl

DEFAULT_THRESHOLD = 0.7

def build_label():
    return "front"

@dsl
def unresolved_defaults(
    threshold=DEFAULT_THRESHOLD,
    items=[],
    label=build_label(),
    maybe=None,
):
    return threshold, items, label, maybe
""".strip(),
    )

    args = {arg.name: arg for arg in steps["unresolved_defaults"].arguments}

    assert args["threshold"].type_name == "Any"
    assert args["items"].type_name == "Any"
    assert args["label"].type_name == "Any"
    assert args["maybe"].type_name == "Any"


def test_discovers_library_steps_from_stub_only_modules(tmp_path: Path):
    stub_file = tmp_path / "raccoon" / "step" / "motion" / "drive_dsl.pyi"
    stub_file.parent.mkdir(parents=True)
    stub_file.write_text(
        """
from raccoon import dsl

@dsl
def local_drive(speed: float = 0.5): ...
""".strip(),
        encoding="utf-8",
    )

    analyzer = DSLStepAnalyzer(tmp_path)
    steps = analyzer.analyze_all_steps()
    names = {step.name for step in steps}

    assert "local_drive" in names
