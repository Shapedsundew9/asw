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
