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


def founder_review(
    phase_name: str,
    artifact_path: Path,
    questions: list[dict] | None = None,
) -> tuple[str, str | None]:
    """Pause for Founder review of an artifact.

    Displays a summary and prompts for a decision. If *questions* are provided,
    they are presented first and answering them automatically triggers a "Modify"
    action with the answers as feedback.

    Parameters
    ----------
    phase_name:
        Human-readable name of the current phase (e.g. ``"PRD"``).
    artifact_path:
        Path to the artifact file to review.
    questions:
        Optional list of question objects to ask the Founder.

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

    if questions:
        _console.print("\n[bold yellow]Agent has questions/recommendations for you:[/bold yellow]")
        answers = []
        for i, q in enumerate(questions, 1):
            q_text = q.get("question", "No question text provided.")
            choices = q.get("choices", [])

            if choices:
                # Present choices + "Something else..."
                options = [questionary.Choice(c, value=c) for c in choices]
                options.append(questionary.Choice("Something else...", value="__other__"))
                ans = questionary.select(f"Q{i}: {q_text}", choices=options).ask()

                if ans == "__other__":
                    ans = questionary.text("Please specify:").ask()
            else:
                # Free-form text
                ans = questionary.text(f"Q{i}: {q_text}").ask()

            if ans is None:
                # User aborted
                _console.print("\n[bold red]Pipeline stopped by Founder.[/bold red]")
                sys.exit(0)

            answers.append(f"Q: {q_text}\nA: {ans}")

        feedback_text = "Here are the answers to your questions:\n\n" + "\n\n".join(answers)
        _console.print("[dim]──── Answers captured, automatically modifying artifact ────[/dim]")
        return "m", feedback_text

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
