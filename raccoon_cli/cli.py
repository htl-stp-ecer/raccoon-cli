"""Main CLI entry point for raccoon."""

from __future__ import annotations

import click
from rich.console import Console

from raccoon_cli.commands import (
    calibrate_group,
    codegen_command,
    run_command,
    wizard_command,
    create_command,
    list_command,
    remove_command,
    connect_command,
    disconnect_command,
    sync_command,
    lcm_group,
    web_command,
    update_command,
    checkpoint_group,
    reorder_command,
    logs_group,
    migrate_command,
    validate_command,
    shell_command,
    doctor_command,
    upload_command,
)
from raccoon_cli.logging_utils import configure_logging, render_banner, render_summary

CONTEXT_SETTINGS = {
    "help_option_names": ["-h", "--help"],
}

# Commands that don't operate on a project — skip auto-validation.
_SKIP_VALIDATE_COMMANDS = {"validate", "create", "connect", "disconnect", "update", "doctor", "migrate", "web", "upload"}


def _normalize_exit_code(code) -> int:
    if code is None:
        return 0
    if isinstance(code, bool):
        return 1 if code else 0
    if isinstance(code, int):
        return code
    return 1


class RaccoonGroup(click.Group):
    def invoke(self, ctx: click.Context):
        ctx.ensure_object(dict)
        try:
            result = super().invoke(ctx)
        except click.exceptions.Exit as exc:
            ctx.obj["exit_code"] = _normalize_exit_code(getattr(exc, "exit_code", None))
            raise
        except SystemExit as exc:
            ctx.obj["exit_code"] = _normalize_exit_code(exc.code)
            raise
        except click.ClickException:
            ctx.obj["exit_code"] = 1
            raise
        except Exception:
            ctx.obj["exit_code"] = 1
            raise
        else:
            ctx.obj["exit_code"] = 0
            return result


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
    ctx.obj.setdefault("exit_code", None)
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

    exit_code = ctx.obj.get("exit_code")
    if not summary.has_messages() and exit_code not in (None, 0):
        summary.clear()
        ctx.obj["summary_printed"] = True
        return

    render_summary(console, summary)
    summary.clear()
    ctx.obj["summary_printed"] = True


def _run_auto_validate(ctx: click.Context) -> None:
    """Run project validation before the subcommand if we're inside a project."""
    if ctx.obj.get("no_validate"):
        return
    if ctx.invoked_subcommand in _SKIP_VALIDATE_COMMANDS:
        return

    from raccoon_cli.project import ProjectError, find_project_root
    from raccoon_cli.validation import run_validation_or_exit

    try:
        project_root = find_project_root()
    except ProjectError:
        return  # not in a project — let the subcommand handle it

    run_validation_or_exit(ctx.obj["console"], project_root)


@click.group(context_settings=CONTEXT_SETTINGS, no_args_is_help=True, cls=RaccoonGroup)
@click.option("--no-validate", is_flag=True, help="Skip pre-command project validation.")
@click.pass_context
def main(ctx: click.Context, no_validate: bool) -> None:
    """Raccoon - Toolchain CLI for raccoon projects."""
    _setup_context(ctx)
    ctx.obj["no_validate"] = no_validate
    _run_auto_validate(ctx)


main.add_command(calibrate_group)
main.add_command(codegen_command)
main.add_command(run_command)
main.add_command(wizard_command)
main.add_command(create_command)
main.add_command(list_command)
main.add_command(remove_command)
main.add_command(connect_command)
main.add_command(disconnect_command)
main.add_command(sync_command)
main.add_command(lcm_group)
main.add_command(web_command)
main.add_command(update_command)
main.add_command(checkpoint_group)
main.add_command(reorder_command)
main.add_command(logs_group)
main.add_command(migrate_command)
main.add_command(validate_command)
main.add_command(shell_command)
main.add_command(doctor_command)
main.add_command(upload_command)


if __name__ == "__main__":
    main()
