"""The Pi server's calibrate command line must be accepted by the CLI."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from raccoon_cli.commands import calibrate
from raccoon_cli.server.routes import commands


@pytest.mark.asyncio
@pytest.mark.parametrize("subcommand", ["ticks", "autotune", "servos", "step-response"])
async def test_server_calibrate_args_run_locally(subcommand):
    with patch.object(commands, "_start_command", new=AsyncMock()) as start:
        await commands.calibrate_project(
            "proj", commands.CommandRequest(args=[subcommand])
        )
    argv = start.await_args.kwargs["args"]
    assert argv[0] == "calibrate"

    assert "--no-codegen" in argv

    seen = []

    def _prepare(ctx, local):
        seen.append(local)
        ctx.obj.update(console=MagicMock(), project_root=Path(), config={}, local=local)

    with patch.object(calibrate, "_prepare_calibration_context", _prepare), \
            patch.object(calibrate, "_calibrate_ticks"), \
            patch.object(calibrate, "_run_autotune"), \
            patch.object(calibrate, "_calibrate_servos"), \
            patch.object(calibrate, "_run_step_response_local") as step_response, \
            patch.object(calibrate, "_plot_step_response"):
        result = CliRunner().invoke(calibrate.calibrate_group, argv[1:], obj={})

    assert result.exit_code == 0, result.output
    assert seen == [True]
    if subcommand == "step-response":
        assert step_response.call_args.kwargs["codegen"] is False


@pytest.mark.parametrize("codegen", [True, False])
def test_autotune_local_generates_code_only_when_asked(tmp_path, codegen):
    with patch.object(calibrate, "create_pipeline") as pipeline, \
            patch.object(calibrate.subprocess, "Popen") as popen:
        popen.return_value.wait.return_value = 0
        calibrate._autotune_local(MagicMock(), tmp_path, {}, codegen=codegen)

    assert pipeline.called is codegen
