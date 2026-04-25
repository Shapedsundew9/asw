"""Tests for validation-runner helpers."""

from __future__ import annotations

import sys
from pathlib import Path

from asw.validation_runner import render_validation_report_markdown, run_validation_contract


def _python_command(script: str) -> str:
    """Return a shell command that runs *script* with the current Python interpreter."""
    return f'"{sys.executable}" -c "{script}"'


def test_run_validation_contract_executes_command_in_working_directory(tmp_path: Path) -> None:
    """Command validations should run in the configured working directory."""
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    contract = {
        "validations": [
            {
                "id": "pwd_check",
                "title": "PWD check",
                "kind": "command",
                "command": _python_command("import os, sys; sys.stdout.write(os.getcwd())"),
                "working_directory": "app",
                "success_criteria": ["pwd exits with 0"],
                "protects": ["Validation runner cwd handling"],
                "always_run": True,
                "enabled": True,
            }
        ]
    }

    report = run_validation_contract(contract, workspace=tmp_path)

    assert report.passed is True
    assert len(report.results) == 1
    assert report.results[0].status == "passed"
    assert report.results[0].stdout.strip() == str(app_dir)


def test_run_validation_contract_marks_failed_command_checks(tmp_path: Path) -> None:
    """Failing commands should preserve stderr and non-zero exit codes."""
    contract = {
        "validations": [
            {
                "id": "failing_check",
                "title": "Failing check",
                "kind": "command",
                "command": _python_command("import sys; sys.stderr.write('broken'); raise SystemExit(3)"),
                "working_directory": ".",
                "success_criteria": ["Command exits with 0"],
                "protects": ["Failure reporting"],
                "always_run": True,
                "enabled": True,
            }
        ]
    }

    report = run_validation_contract(contract, workspace=tmp_path)

    assert report.passed is False
    assert report.results[0].status == "failed"
    assert report.results[0].exit_code == 3
    assert report.results[0].stderr == "broken"


def test_run_validation_contract_marks_manual_validations_pending(tmp_path: Path) -> None:
    """Checklist and manual-gate validations should be surfaced as pending obligations."""
    contract = {
        "validations": [
            {
                "id": "manual_smoke",
                "title": "Manual smoke test",
                "kind": "manual_gate",
                "success_criteria": ["Founder confirms the flow works"],
                "protects": ["Manual founder approval"],
                "always_run": True,
                "enabled": True,
            },
            {
                "id": "release_checklist",
                "title": "Release checklist",
                "kind": "checklist",
                "success_criteria": ["Checklist is reviewed"],
                "protects": ["Release readiness"],
                "always_run": False,
                "enabled": True,
            },
        ]
    }

    report = run_validation_contract(contract, workspace=tmp_path)

    assert report.passed is True
    assert report.has_pending_manual_validations is True
    assert [result.status for result in report.results] == ["pending", "pending"]


def test_render_validation_report_markdown_includes_summary_and_statuses(tmp_path: Path) -> None:
    """Rendered reports should summarize pass, fail, and pending results."""
    contract = {
        "validations": [
            {
                "id": "passing_check",
                "title": "Passing check",
                "kind": "command",
                "command": _python_command("import sys; sys.stdout.write('ok')"),
                "working_directory": ".",
                "success_criteria": ["Command exits with 0"],
                "protects": ["Happy path"],
                "always_run": True,
                "enabled": True,
            },
            {
                "id": "manual_smoke",
                "title": "Manual smoke test",
                "kind": "manual_gate",
                "success_criteria": ["Founder confirms the flow works"],
                "protects": ["Manual founder approval"],
                "always_run": True,
                "enabled": True,
            },
        ]
    }

    report = run_validation_contract(contract, workspace=tmp_path)
    markdown = render_validation_report_markdown(report, report_title="phase_1 turn_01")

    assert "# Validation Report: phase_1 turn_01" in markdown
    assert "- **Passed:** 1" in markdown
    assert "- **Pending Manual Checks:** 1" in markdown
    assert "- **Status:** passed" in markdown
    assert "- **Status:** pending" in markdown
