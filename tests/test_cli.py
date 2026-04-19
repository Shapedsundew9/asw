"""Tests for CLI help output and parsing."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from asw.cli.main import build_parser, main


def test_top_level_help_mentions_command_specific_help() -> None:
    """Top-level help should explain how to inspect subcommand options."""
    help_text = " ".join(build_parser().format_help().split())

    assert "Use 'asw <command> --help' for command-specific options." in help_text
    assert "Example: 'asw start --help'." in help_text


def test_start_help_lists_supported_flags(capsys: pytest.CaptureFixture[str]) -> None:
    """The ``start`` subcommand help should enumerate its available flags."""
    parser = build_parser()

    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["start", "--help"])

    assert excinfo.value.code == 0

    start_help = capsys.readouterr().out

    assert "--vision VISION" in start_help
    assert "--workdir WORKDIR" in start_help
    assert "--no-commit" in start_help
    assert "--stage-all" in start_help
    assert "--restart" in start_help
    assert "--debug [LOGFILE]" in start_help


def test_main_passes_stage_all_to_pipeline(tmp_path: Path) -> None:
    """CLI should forward ``--stage-all`` to the pipeline."""
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n")

    with patch("asw.orchestrator.run_pipeline", return_value=0) as mock_run_pipeline:
        result = main(
            [
                "start",
                "--vision",
                str(vision),
                "--workdir",
                str(tmp_path),
                "--no-commit",
                "--stage-all",
            ]
        )

    assert result == 0
    assert mock_run_pipeline.call_args.kwargs["options"].stage_all is True


def test_main_defaults_stage_all_to_false(tmp_path: Path) -> None:
    """CLI should default to ``stage_all=False`` when the flag is omitted."""
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n")

    with patch("asw.orchestrator.run_pipeline", return_value=0) as mock_run_pipeline:
        result = main(
            [
                "start",
                "--vision",
                str(vision),
                "--workdir",
                str(tmp_path),
                "--no-commit",
            ]
        )

    assert result == 0
    assert mock_run_pipeline.call_args.kwargs["options"].stage_all is False


def test_main_creates_missing_debug_log_directory(tmp_path: Path) -> None:
    """CLI should create missing parent directories for custom debug log paths."""
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n")
    missing_log = tmp_path / "logs" / "asw.log"

    with patch("asw.orchestrator.run_pipeline", return_value=0):
        result = main(
            [
                "start",
                "--vision",
                str(vision),
                "--workdir",
                str(tmp_path),
                "--debug",
                str(missing_log),
                "--no-commit",
            ]
        )

    assert result == 0
    assert missing_log.parent.is_dir()
