"""Tests for the Founder Review Gate."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from asw.gates import founder_review


def test_approve(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# Test\n\nSome content.\n")

    with patch("builtins.input", return_value="a"):
        choice, feedback = founder_review("Test Phase", artifact)

    assert choice == "a"
    assert feedback is None


def test_reject(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# Test\n\nContent.\n")

    with patch("builtins.input", return_value="r"):
        choice, feedback = founder_review("Test Phase", artifact)

    assert choice == "r"
    assert feedback is None


def test_modify(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# Test\n\nContent.\n")

    responses = iter(["m", "Fix the intro section", ""])
    with patch("builtins.input", side_effect=responses):
        choice, feedback = founder_review("Test Phase", artifact)

    assert choice == "m"
    assert feedback == "Fix the intro section"


def test_invalid_then_valid(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# Test\n\nContent.\n")

    responses = iter(["z", "q", "a"])
    with patch("builtins.input", side_effect=responses):
        choice, _feedback = founder_review("Test Phase", artifact)

    assert choice == "a"
