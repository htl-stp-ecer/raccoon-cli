"""Shell completion installation command."""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

import click
import yaml

# State file for tracking CLI preferences
STATE_FILE_PATH = Path.home() / ".raccoon" / "cli_state.yml"


def _load_state() -> dict:
    """Load CLI state from file."""
    if not STATE_FILE_PATH.exists():
        return {}
    try:
        with open(STATE_FILE_PATH) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    """Save CLI state to file."""
    STATE_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE_PATH, "w") as f:
        yaml.safe_dump(state, f, default_flow_style=False)


def completion_already_offered() -> bool:
    """Check if completion setup has already been offered to the user."""
    state = _load_state()
    return state.get("completion_offered", False)


def mark_completion_offered() -> None:
    """Mark that completion setup has been offered to the user."""
    state = _load_state()
    state["completion_offered"] = True
    _save_state(state)


def is_completion_installed() -> bool:
    """Check if shell completion appears to be installed."""
    shell = _get_shell()
    if shell is None:
        return False

    completion_file = _get_completion_file(shell)
    return completion_file.exists()


def prompt_completion_setup() -> None:
    """Prompt user to set up shell completion on first run.

    This should be called during CLI startup to offer completion installation.
    """
    # Don't prompt if already offered or already installed
    if completion_already_offered() or is_completion_installed():
        mark_completion_offered()  # Ensure it's marked
        return

    shell = _get_shell()
    if shell is None:
        # Can't detect shell, skip prompt
        mark_completion_offered()
        return

    # Ask user if they want to install completion
    click.echo()
    click.secho("Shell completion available!", fg="cyan", bold=True)
    click.echo(f"Detected shell: {shell}")
    click.echo()

    if click.confirm("Would you like to enable tab-completion for raccoon?", default=True):
        try:
            click.echo()
            click.echo(f"Generating completion script for {shell}...")
            completion_script = _get_completion_script(shell)

            if shell in ("bash", "zsh"):
                message = _install_bash_zsh(shell, completion_script)
            elif shell == "fish":
                message = _install_fish(completion_script)
            elif shell == "powershell":
                message = _install_powershell(completion_script)
            else:
                message = f"Unsupported shell: {shell}"

            click.echo(message)
        except Exception as e:
            click.secho(f"Failed to install completion: {e}", fg="red")
            click.echo("You can try again later with: raccoon completion install")
    else:
        click.echo("Skipped. You can install later with: raccoon completion install")

    mark_completion_offered()
    click.echo()


def _get_shell() -> str | None:
    """Detect the current shell."""
    if platform.system() == "Windows":
        # Check if running in PowerShell
        if os.environ.get("PSModulePath"):
            return "powershell"
        return "powershell"  # Default to PowerShell on Windows

    # Unix-like systems
    shell = os.environ.get("SHELL", "")
    if "zsh" in shell:
        return "zsh"
    elif "bash" in shell:
        return "bash"
    elif "fish" in shell:
        return "fish"
    return None


def _is_valid_completion_script(output: str, shell: str) -> bool:
    """Check if the output looks like a valid completion script."""
    if not output:
        return False
    # Check for shell-specific completion script markers
    markers = {
        "bash": "_raccoon_completion",
        "zsh": "#compdef raccoon",
        "fish": "complete -c raccoon",
        "powershell": "Register-ArgumentCompleter",
    }
    marker = markers.get(shell)
    return marker is not None and marker in output


def _get_completion_script(shell: str) -> str:
    """Generate the completion script for the given shell."""
    # Try running raccoon directly first (most reliable)
    raccoon_path = Path(sys.executable).parent / "raccoon"
    if raccoon_path.exists():
        result = subprocess.run(
            [str(raccoon_path)],
            env={**os.environ, "_RACCOON_COMPLETE": f"{shell}_source"},
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and _is_valid_completion_script(result.stdout, shell):
            return result.stdout

    # Fallback: try python -m raccoon.cli
    result = subprocess.run(
        [sys.executable, "-m", "raccoon.cli"],
        env={**os.environ, "_RACCOON_COMPLETE": f"{shell}_source"},
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and _is_valid_completion_script(result.stdout, shell):
        return result.stdout

    raise click.ClickException(f"Failed to generate completion script for {shell}")


def _get_rc_file(shell: str) -> Path:
    """Get the shell RC file path."""
    home = Path.home()

    if shell == "bash":
        # Prefer .bashrc, fall back to .bash_profile on macOS
        bashrc = home / ".bashrc"
        if bashrc.exists() or platform.system() != "Darwin":
            return bashrc
        return home / ".bash_profile"
    elif shell == "zsh":
        return home / ".zshrc"
    elif shell == "fish":
        config_dir = home / ".config" / "fish" / "completions"
        config_dir.mkdir(parents=True, exist_ok=True)
        return config_dir / "raccoon.fish"
    elif shell == "powershell":
        # PowerShell profile path
        result = subprocess.run(
            ["powershell", "-Command", "echo $PROFILE"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            profile_path = Path(result.stdout.strip())
            profile_path.parent.mkdir(parents=True, exist_ok=True)
            return profile_path
        # Fallback
        return home / "Documents" / "WindowsPowerShell" / "Microsoft.PowerShell_profile.ps1"

    raise click.ClickException(f"Unknown shell: {shell}")


def _get_completion_file(shell: str) -> Path:
    """Get the path where completion script will be saved."""
    home = Path.home()

    if shell == "fish":
        # Fish uses a completions directory
        return home / ".config" / "fish" / "completions" / "raccoon.fish"
    elif shell == "powershell":
        return home / ".raccoon-complete.ps1"
    else:
        return home / f".raccoon-complete.{shell}"


def _install_bash_zsh(shell: str, completion_script: str) -> str:
    """Install completion for bash or zsh."""
    completion_file = _get_completion_file(shell)
    rc_file = _get_rc_file(shell)

    # Save completion script
    completion_file.write_text(completion_script)

    # Add source line to RC file if not already present
    source_line = f'source "{completion_file}"'

    rc_content = rc_file.read_text() if rc_file.exists() else ""
    if str(completion_file) not in rc_content:
        with rc_file.open("a") as f:
            f.write(f"\n# Raccoon CLI completion\n{source_line}\n")
        return f"Installed! Completion script saved to {completion_file}\nAdded source line to {rc_file}\n\nRun 'source {rc_file}' or restart your terminal."
    else:
        return f"Updated completion script at {completion_file}\nSource line already in {rc_file}"


def _install_fish(completion_script: str) -> str:
    """Install completion for fish."""
    completion_file = _get_completion_file("fish")
    completion_file.parent.mkdir(parents=True, exist_ok=True)
    completion_file.write_text(completion_script)
    return f"Installed! Completion script saved to {completion_file}\nFish will load it automatically."


def _install_powershell(completion_script: str) -> str:
    """Install completion for PowerShell."""
    completion_file = _get_completion_file("powershell")
    rc_file = _get_rc_file("powershell")

    # Save completion script
    completion_file.parent.mkdir(parents=True, exist_ok=True)
    completion_file.write_text(completion_script)

    # Add source line to profile if not already present
    source_line = f'. "{completion_file}"'

    rc_content = rc_file.read_text() if rc_file.exists() else ""
    if str(completion_file) not in rc_content:
        rc_file.parent.mkdir(parents=True, exist_ok=True)
        with rc_file.open("a") as f:
            f.write(f"\n# Raccoon CLI completion\n{source_line}\n")
        return f"Installed! Completion script saved to {completion_file}\nAdded source line to {rc_file}\n\nRestart PowerShell to enable completion."
    else:
        return f"Updated completion script at {completion_file}\nSource line already in {rc_file}"


@click.group(name="completion")
def completion_group() -> None:
    """Manage shell tab-completion for raccoon CLI."""
    pass


@completion_group.command(name="install")
@click.option(
    "--shell",
    type=click.Choice(["bash", "zsh", "fish", "powershell"]),
    help="Shell to install completion for. Auto-detected if not specified.",
)
def install_completion(shell: str | None) -> None:
    """Install shell tab-completion.

    Automatically detects your shell and installs the appropriate
    completion script. Supports bash, zsh, fish, and PowerShell.
    """
    if shell is None:
        shell = _get_shell()
        if shell is None:
            raise click.ClickException(
                "Could not detect shell. Please specify with --shell"
            )
        click.echo(f"Detected shell: {shell}")

    click.echo(f"Generating completion script for {shell}...")
    completion_script = _get_completion_script(shell)

    if shell in ("bash", "zsh"):
        message = _install_bash_zsh(shell, completion_script)
    elif shell == "fish":
        message = _install_fish(completion_script)
    elif shell == "powershell":
        message = _install_powershell(completion_script)
    else:
        raise click.ClickException(f"Unsupported shell: {shell}")

    click.echo(message)


@completion_group.command(name="show")
@click.option(
    "--shell",
    type=click.Choice(["bash", "zsh", "fish", "powershell"]),
    help="Shell to show completion script for. Auto-detected if not specified.",
)
def show_completion(shell: str | None) -> None:
    """Print the completion script without installing.

    Useful if you want to manually add the completion to your shell config.
    """
    if shell is None:
        shell = _get_shell()
        if shell is None:
            raise click.ClickException(
                "Could not detect shell. Please specify with --shell"
            )

    completion_script = _get_completion_script(shell)
    click.echo(completion_script)
