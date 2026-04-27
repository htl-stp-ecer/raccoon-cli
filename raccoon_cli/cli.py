"""Main CLI entry point for raccoon."""

from __future__ import annotations

import click
from rich.console import Console

from raccoon_cli.commands import (
    calibrate_command,
    codegen_command,
    run_command,
    wizard_command,
    create_command,
    list_command,
    remove_command,
    connect_command,
    disconnect_command,
    status_command,
    sync_command,
    lcm_group,
    web_command,
    update_command,
    checkpoint_group,
    reorder_command,
    logs_group,
    migrate_command,
    validate_command,
)
from raccoon_cli.logging_utils import configure_logging, render_banner, render_summary

CONTEXT_SETTINGS = {
    "help_option_names": ["-h", "--help"],
}


def _setup_context(ctx: click.Context) -> None:
    """Ensure console and logging are ready for a command invocation."""
    ctx.ensure_object(dict)

    if not ctx.obj.get("initialized"):
        console = Console()
        summary = configure_logging(console)
        ctx.obj["console"] = console
        ctx.obj["log_summary"] = summary
        ctx.obj["initialized"] = True
        render_banner(console)
    else:
        summary = ctx.obj["log_summary"]

    summary.clear()
    ctx.obj["summary_printed"] = False
    if not ctx.obj.get("summary_registered"):
        ctx.call_on_close(lambda: _print_summary(ctx))
        ctx.obj["summary_registered"] = True


def _print_summary(ctx: click.Context) -> None:
    """Render the warning/error summary exactly once per command."""
    if ctx.obj.get("summary_printed"):
        return

    console: Console | None = ctx.obj.get("console")
    summary = ctx.obj.get("log_summary")
    if console is None or summary is None:
        return

    render_summary(console, summary)
    summary.clear()
    ctx.obj["summary_printed"] = True


@click.group(context_settings=CONTEXT_SETTINGS, no_args_is_help=True)
@click.pass_context
def main(ctx: click.Context) -> None:
    """Raccoon - Toolchain CLI for raccoon projects."""
    _setup_context(ctx)


main.add_command(calibrate_command)
main.add_command(codegen_command)
main.add_command(run_command)
main.add_command(wizard_command)
main.add_command(create_command)
main.add_command(list_command)
main.add_command(remove_command)
main.add_command(connect_command)
main.add_command(disconnect_command)
main.add_command(status_command)
main.add_command(sync_command)
main.add_command(lcm_group)
main.add_command(web_command)
main.add_command(update_command)
main.add_command(checkpoint_group)
main.add_command(reorder_command)
main.add_command(logs_group)
main.add_command(migrate_command)
main.add_command(validate_command)


if __name__ == "__main__":
    main()
