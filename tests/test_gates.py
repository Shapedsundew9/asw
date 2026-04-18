"""Tests for the Founder Review Gate."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from asw.gates import founder_review


def test_approve(tmp_path: Path) -> None:
    """Test founder approval returns 'a' with no feedback."""
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# Test\n\nSome content.\n")

    with patch("asw.gates.questionary.select") as mock_select:
        mock_select.return_value.ask.return_value = "a"
        choice, feedback = founder_review("Test Phase", artifact)

    assert choice == "a"
    assert feedback is None


def test_reject(tmp_path: Path) -> None:
    """Test founder rejection returns 'r' with no feedback."""
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# Test\n\nContent.\n")

    with patch("asw.gates.questionary.select") as mock_select:
        mock_select.return_value.ask.return_value = "r"
        choice, feedback = founder_review("Test Phase", artifact)

    assert choice == "r"
    assert feedback is None


def test_modify(tmp_path: Path) -> None:
    """Test founder modification returns 'm' with feedback text."""
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# Test\n\nContent.\n")

    with (
        patch("asw.gates.questionary.select") as mock_select,
        patch("asw.gates.questionary.text") as mock_text,
    ):
        mock_select.return_value.ask.return_value = "m"
        mock_text.return_value.ask.return_value = "Fix the intro section"
        choice, feedback = founder_review("Test Phase", artifact)

    assert choice == "m"
    assert feedback == "Fix the intro section"


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
