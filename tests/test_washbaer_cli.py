from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from raccoon_cli.cli import washbaer_main


def test_washbaer_help_lists_german_run_alias() -> None:
    runner = CliRunner()
    result = runner.invoke(washbaer_main, ["--help"])

    assert result.exit_code == 0
    expected_aliases = (
        "kalibrieren",
        "codebau",
        "lauf",
        "zauber",
        "erstellen",
        "liste",
        "entfernen",
        "verbinden",
        "trennen",
        "abgleich",
        "netz",
        "aktualisieren",
        "pruefpunkt",
        "neuordnen",
        "protokolle",
    )
    for alias in expected_aliases:
        assert alias in result.output


def test_washbaer_lauf_accepts_entwicklung_flag() -> None:
    runner = CliRunner()
    fake_project_root = Path("/tmp/fake-raccoon-project")
    fake_ctx = MagicMock()
    fake_ctx.obj = {"console": MagicMock()}

    with patch("raccoon_cli.commands.run.require_project", return_value=fake_project_root), \
         patch("raccoon_cli.commands.run.load_project_config", return_value={"name": "Demo", "uuid": "demo-uuid"}), \
         patch("raccoon_cli.commands.run._run_local") as mock_run_local:
        result = runner.invoke(washbaer_main, ["lauf", "--entwicklung", "--lokal"], obj=fake_ctx.obj)

    assert result.exit_code == 0
    assert mock_run_local.call_count == 1
    _, kwargs = mock_run_local.call_args
    assert kwargs["dev"] is True
