"""Founder Review Gate – terminal-based approval workflow."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import questionary
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

logger = logging.getLogger("asw.gates")

_console = Console()


def founder_review(phase_name: str, artifact_path: Path) -> tuple[str, str | None]:
    """Pause for Founder review of an artifact.

    Displays a summary and prompts for a decision.

    Parameters
    ----------
    phase_name:
        Human-readable name of the current phase (e.g. ``"PRD"``).
    artifact_path:
        Path to the artifact file to review.

    Returns:
    -------
    tuple[str, str | None]
        ``(choice, feedback)`` where *choice* is one of
        ``"a"`` (approve), ``"r"`` (reject), ``"m"`` (modify), ``"s"`` (stop)
        and *feedback* is the Founder's text when *choice* is ``"m"``, else ``None``.
    """
    content = artifact_path.read_text(encoding="utf-8")
    md = Markdown(content)

    _console.print(
        Panel(
            md,
            title=f"[bold blue]FOUNDER REVIEW GATE – Phase: {phase_name}[/bold blue]",
            subtitle=f"[dim]Artifact: {artifact_path}[/dim]",
            border_style="blue",
        )
    )

    choice = questionary.select(
        "Founder Action:",
        choices=[
            questionary.Choice("Approve", value="a", shortcut_key="a"),
            questionary.Choice("Reject", value="r", shortcut_key="r"),
            questionary.Choice("Modify", value="m", shortcut_key="m"),
            questionary.Choice("Stop", value="s", shortcut_key="s"),
        ],
    ).ask()

    # questionary returns None if the user aborts via Ctrl-C
    if choice is None:
        choice = "s"

    logger.debug("Founder review for %s: choice=%s", phase_name, choice)

    feedback: str | None = None
    if choice == "m":
        feedback = questionary.text(
            "Enter your feedback (press ESC then ENTER to submit):",
            multiline=True,
        ).ask()

        if feedback is None:
            # Aborted
            choice = "s"
        else:
            feedback = feedback.strip()
            _console.print("[dim]──── Feedback captured ────[/dim]")
            logger.debug("Founder feedback for %s:\n%s", phase_name, feedback)

    if choice == "s":
        _console.print("\n[bold red]Pipeline stopped by Founder.[/bold red]")
        sys.exit(0)

    return choice, feedback
