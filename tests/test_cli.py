"""Tests for CLI help output and parsing."""

from __future__ import annotations

import pytest

from asw.cli.main import build_parser


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
    assert "--restart" in start_help
    assert "--debug [LOGFILE]" in start_help
