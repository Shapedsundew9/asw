"""Tests for the Founder Review Gate."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from asw.gates import founder_review


def test_approve(tmp_path: Path) -> None:
    """Test founder approval returns an approve action with no feedback."""
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# Test\n\nSome content.\n")

    with patch("asw.gates.questionary.select") as mock_select:
        mock_select.return_value.ask.return_value = "approve"
        review = founder_review("Test Phase", artifact)

    assert review.action == "approve"
    assert review.feedback is None
    assert not review.answers


def test_reject(tmp_path: Path) -> None:
    """Test founder rejection returns a reject action with no feedback."""
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# Test\n\nContent.\n")

    with patch("asw.gates.questionary.select") as mock_select:
        mock_select.return_value.ask.return_value = "reject"
        review = founder_review("Test Phase", artifact)

    assert review.action == "reject"
    assert review.feedback is None
    assert not review.answers


def test_modify(tmp_path: Path) -> None:
    """Test founder modification returns structured feedback."""
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# Test\n\nContent.\n")

    with (
        patch("asw.gates.questionary.select") as mock_select,
        patch("asw.gates.questionary.text") as mock_text,
    ):
        mock_select.return_value.ask.return_value = "modify"
        mock_text.return_value.ask.return_value = "Fix the intro section"
        review = founder_review("Test Phase", artifact)

    assert review.action == "modify"
    assert review.feedback == "Fix the intro section"
    assert not review.answers


def test_execution_plan_modify_prompt_mentions_json(tmp_path: Path) -> None:
    """Execution-plan modify prompts should advertise the direct JSON shortcut."""
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# Execution Plan\n\nContent.\n")

    with (
        patch("asw.gates.questionary.select") as mock_select,
        patch("asw.gates.questionary.text") as mock_text,
    ):
        mock_select.return_value.ask.return_value = "modify"
        mock_text.return_value.ask.return_value = "{}"
        review = founder_review("Execution Plan", artifact)

    assert review.action == "modify"
    assert "paste a full execution-plan JSON object" in mock_text.call_args.args[0]


def test_request_more_questions(tmp_path: Path) -> None:
    """Test founder can explicitly request another question round."""
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# Test\n\nContent.\n")

    with (
        patch("asw.gates.questionary.select") as mock_select,
        patch("asw.gates.questionary.text") as mock_text,
    ):
        mock_select.return_value.ask.return_value = "request_more_questions"
        mock_text.return_value.ask.return_value = "Ask about deployment constraints"
        review = founder_review("Test Phase", artifact)

    assert review.action == "request_more_questions"
    assert review.feedback == "Ask about deployment constraints"
    assert not review.answers


def test_abort(tmp_path: Path) -> None:
    """Test abort via questionary returning None becomes stop."""
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# Test\n\nContent.\n")

    with (
        patch("asw.gates.sys.exit") as mock_exit,
        patch("asw.gates.questionary.select") as mock_select,
    ):
        mock_select.return_value.ask.return_value = None
        founder_review("Test Phase", artifact)
        mock_exit.assert_called_once_with(0)


def test_founder_questions(tmp_path: Path) -> None:
    """Test answering founder questions returns structured question-answer pairs."""
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# Test\n\nContent.\n")
    questions = [
        {"question": "DB choice?", "choices": ["PG", "Lite"]},
        {"question": "Project name?"},
    ]

    with (
        patch("asw.gates.questionary.select") as mock_select,
        patch("asw.gates.questionary.text") as mock_text,
    ):
        mock_select.return_value.ask.return_value = "PG"
        mock_text.return_value.ask.return_value = "My Project"
        review = founder_review("Test Phase", artifact, questions=questions)

    assert review.action == "answer_questions"
    assert review.feedback is None
    assert review.answers == [
        {"question": "DB choice?", "answer": "PG"},
        {"question": "Project name?", "answer": "My Project"},
    ]


def test_founder_review_hides_pending_question_sections_and_json(tmp_path: Path) -> None:
    """Founder review should not render pending question prose or raw JSON blocks."""
    artifact = tmp_path / "artifact.md"
    artifact.write_text(
        "## Open Questions\n\n"
        "1. Which database should we use?\n"
        '   - Choices: ["PostgreSQL", "SQLite"]\n\n'
        "```json\n"
        '{"founder_questions": [{"question": "Which database should we use?", "choices": ["PostgreSQL", "SQLite"]}]}'
        "\n```\n",
        encoding="utf-8",
    )

    with (
        patch("asw.gates.Markdown") as mock_markdown,
        patch("asw.gates.questionary.select") as mock_select,
    ):
        mock_markdown.side_effect = lambda text: text
        mock_select.return_value.ask.return_value = "PostgreSQL"
        founder_review(
            "PRD",
            artifact,
            questions=[{"question": "Which database should we use?", "choices": ["PostgreSQL", "SQLite"]}],
        )

    rendered = mock_markdown.call_args.args[0]
    assert "Which database should we use?" not in rendered
    assert "```json" not in rendered
