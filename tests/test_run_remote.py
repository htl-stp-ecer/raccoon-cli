"""Tests for the laptop's remote run output."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from raccoon_cli.commands.run import _run_remote


async def test_raw_mode_streams_remote_output_and_keeps_project_arguments(tmp_path, capsys):
    ctx = SimpleNamespace(obj={"console": MagicMock()})
    connection = SimpleNamespace(
        pi_address="wombat.example", pi_port=8421, api_token=None, pi_hostname="Wombat"
    )
    client = MagicMock()
    client.get_contract = AsyncMock(return_value={})
    client.run_project = AsyncMock(
        return_value=SimpleNamespace(command_id="command-id", service_deployments=[])
    )
    handler = MagicMock()

    def stream_output(console, on_line, echo):
        assert echo is False
        on_line("robot output")
        return {"status": "completed", "exit_code": 0}

    handler.stream_to_console.side_effect = stream_output
    run_id = "20261126T105030Z"

    with (
        patch("raccoon_cli.client.connection.get_connection_manager",
              return_value=SimpleNamespace(state=connection)),
        patch("raccoon_cli.client.api.create_api_client") as create_client,
        patch("raccoon_cli.client.output_handler.OutputHandler", return_value=handler),
        patch("raccoon_cli.commands.run._ping_until_ready", return_value=True),
        patch("raccoon_cli.commands.sync_cmd.sync_project_interactive"),
        patch("rich.live.Live") as live,
    ):
        create_client.return_value.__aenter__.return_value = client
        await _run_remote(
            ctx,
            tmp_path,
            {"name": "test", "uuid": "uuid", "auto_checkpoints": False},
            ("argument with spaces",),
            no_codegen=True,
            no_sync=True,
            record_localization=True,
            run_id=run_id,
            raw=True,
        )

    live.assert_not_called()
    assert capsys.readouterr().out == "robot output\n"
    client.run_project.assert_awaited_once()
    assert client.run_project.call_args.args == ("uuid",)
    assert client.run_project.call_args.kwargs["args"] == ["argument with spaces"]
    assert client.run_project.call_args.kwargs["env"]["LIBSTP_RECORDING_PATH"] == (
        f".raccoon/runs/{run_id}/localization.jsonl"
    )
