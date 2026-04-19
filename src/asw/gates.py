"""Founder Review Gate – terminal-based approval workflow."""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, NoReturn

import questionary
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

logger = logging.getLogger("asw.gates")

_console = Console()

FounderAction = Literal[
    "approve",
    "reject",
    "modify",
    "answer_questions",
    "request_more_questions",
]


@dataclass(frozen=True)
class FounderReviewResult:
    """Structured result returned by the Founder review gate."""

    action: FounderAction
    feedback: str | None = None
    answers: list[dict[str, str]] = field(default_factory=list)


def _stop_pipeline() -> NoReturn:
    """Stop the pipeline at the Founder gate."""
    _console.print("\n[bold red]Pipeline stopped by Founder.[/bold red]")
    sys.exit(0)


def _ask_founder_question(index: int, question: dict) -> str | None:
    """Ask a single founder question and return the selected or typed answer."""
    question_text = question.get("question", "No question text provided.")
    choices = question.get("choices", [])

    if choices:
        options = [questionary.Choice(choice, value=choice) for choice in choices]
        options.append(questionary.Choice("Something else...", value="__other__"))
        answer = questionary.select(f"Q{index}: {question_text}", choices=options).ask()
        if answer == "__other__":
            return questionary.text("Please specify:").ask()
        return answer

    return questionary.text(f"Q{index}: {question_text}").ask()


def _capture_founder_answers(phase_name: str, questions: list[dict]) -> FounderReviewResult:
    """Capture structured answers for the provided founder questions."""
    _console.print("\n[bold yellow]Agent has questions/recommendations for you:[/bold yellow]")
    answers: list[dict[str, str]] = []

    for index, question in enumerate(questions, 1):
        answer = _ask_founder_question(index, question)
        if answer is None:
            _stop_pipeline()

        answers.append(
            {
                "question": question.get("question", "No question text provided."),
                "answer": answer,
            }
        )

    _console.print("[dim]──── Answers captured for local artifact update ────[/dim]")
    logger.debug("Founder answered %d question(s) for %s", len(answers), phase_name)
    return FounderReviewResult(action="answer_questions", answers=answers)


def _prompt_text_feedback(prompt: str) -> str | None:
    """Prompt for optional multiline founder feedback."""
    feedback = questionary.text(prompt, multiline=True).ask()
    if feedback is None:
        return None
    return feedback.strip() or None


def _prompt_founder_action(phase_name: str) -> FounderReviewResult:
    """Prompt for the Founder's next action when no structured questions are pending."""
    choice = questionary.select(
        "Founder Action:",
        choices=[
            questionary.Choice("Approve", value="approve", shortcut_key="a"),
            questionary.Choice("Reject", value="reject", shortcut_key="r"),
            questionary.Choice("Modify", value="modify", shortcut_key="m"),
            questionary.Choice("Request More Questions", value="request_more_questions", shortcut_key="q"),
            questionary.Choice("Stop", value="stop", shortcut_key="s"),
        ],
    ).ask()

    if choice is None or choice == "stop":
        _stop_pipeline()

    logger.debug("Founder review for %s: choice=%s", phase_name, choice)

    if choice == "modify":
        feedback = _prompt_text_feedback("Enter your feedback (press ESC then ENTER to submit):")
        if feedback is None:
            _stop_pipeline()
        _console.print("[dim]──── Feedback captured ────[/dim]")
        logger.debug("Founder feedback for %s:\n%s", phase_name, feedback)
        return FounderReviewResult(action="modify", feedback=feedback)

    if choice == "request_more_questions":
        feedback = _prompt_text_feedback(
            "Optional guidance for the next question round (press ESC then ENTER to submit):"
        )
        _console.print("[dim]──── Follow-up question round requested ────[/dim]")
        if feedback:
            logger.debug("Founder request for more questions in %s:\n%s", phase_name, feedback)
        return FounderReviewResult(action="request_more_questions", feedback=feedback)

    return FounderReviewResult(action=choice)


def founder_review(
    phase_name: str,
    artifact_path: Path,
    questions: list[dict] | None = None,
) -> FounderReviewResult:
    """Pause for Founder review of an artifact.

    Displays a summary and prompts for a decision. If *questions* are provided,
    they are captured first and returned as structured local answers.

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
    FounderReviewResult
        Structured result describing the Founder's next action.
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
        return _capture_founder_answers(phase_name, questions)

    return _prompt_founder_action(phase_name)
