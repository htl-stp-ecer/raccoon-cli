"""Utilities for configuring rich logging with run summaries."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from rich import box
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


@dataclass
class LogSummary:
    """Collects log records for later presentation."""

    records: List[logging.LogRecord] = field(default_factory=list)

    def add(self, record: logging.LogRecord) -> None:
        """Store warning/error records while preserving order."""
        self.records.append(record)

    @property
    def warnings(self) -> List[logging.LogRecord]:
        """Return warning-level records."""
        return [record for record in self.records if record.levelno == logging.WARNING]

    @property
    def errors(self) -> List[logging.LogRecord]:
        """Return error-level and above records."""
        return [record for record in self.records if record.levelno >= logging.ERROR]

    def clear(self) -> None:
        """Remove all stored records."""
        self.records.clear()

    def has_messages(self) -> bool:
        """Check whether warnings or errors were logged."""
        return bool(self.records)


class SummaryHandler(logging.Handler):
    """Logging handler that captures warnings and errors for later display."""

    def __init__(self, summary: LogSummary):
        super().__init__(level=logging.WARNING)
        self.summary = summary

    def emit(self, record: logging.LogRecord) -> None:
        self.summary.add(record)


def configure_logging(
    console: Optional[Console] = None,
    level: int = logging.INFO,
    logger_name: str = "raccoon",
) -> LogSummary:
    """Configure rich logging and return a summary object.

    Args:
        console: Console to route rich output to. Creates a new one if omitted.
        level: Base logging level for the CLI.
        logger_name: Name of the application logger to set level for.

    Returns:
        LogSummary instance that will collect warnings and errors.
    """
    if console is None:
        console = Console()

    summary = LogSummary()

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Replace existing handlers to avoid duplicate messages when the CLI
    # is invoked multiple times from the same process.
    root_logger.handlers.clear()

    rich_handler = RichHandler(
        console=console,
        rich_tracebacks=True,
        show_time=False,
        show_path=False,
        markup=True,
    )
    summary_handler = SummaryHandler(summary)

    root_logger.addHandler(rich_handler)
    root_logger.addHandler(summary_handler)

    app_logger = logging.getLogger(logger_name)
    app_logger.setLevel(level)

    logging.captureWarnings(True)
    return summary


def render_banner(console: Console) -> None:
    """Display an eye-catching banner at CLI start."""
    title = Text("RACCOON TOOLCHAIN", style="bold cyan")
    subtitle = Text("CLI for Raccoon Projects", style="bold white")

    body = Text.from_markup(
        "Generate and run your robot's competition code.\n",
        style="dim white",
    )

    console.print(
        Panel.fit(
            Text.assemble(title, "\n", subtitle, "\n", body),
            border_style="cyan",
            box=box.ROUNDED,
            padding=(1, 4),
        )
    )


def render_summary(console: Console, summary: LogSummary) -> None:
    """Pretty-print the collected warning/error messages."""
    if not summary.has_messages():
        console.print(
            Panel.fit(
                Text("All clear - no warnings or errors logged!", style="bold green"),
                border_style="green",
                box=box.ROUNDED,
            )
        )
        return

    table = Table(
        "Level",
        "Message",
        title="Warnings & Errors",
        title_style="bold yellow",
        box=box.SIMPLE_HEAVY,
        show_edge=False,
        expand=True,
    )

    # To preserve order we walk the stored records once.
    for record in summary.records:
        if record.levelno >= logging.ERROR:
            level_style = "bold red"
        elif record.levelno >= logging.WARNING:
            level_style = "bold yellow"
        else:
            continue
        table.add_row(
            Text(record.levelname, style=level_style),
            Text(record.getMessage(), style="white"),
        )

    console.print(
        Panel(
            table,
            border_style="yellow",
            box=box.ROUNDED,
            padding=(0, 1),
        )
    )
