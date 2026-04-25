"""Execution helpers for validation-contract checks."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("asw.validation_runner")


@dataclass(frozen=True)
class ValidationCheckResult:  # pylint: disable=too-many-instance-attributes
    """The execution result for one validation entry."""

    validation_id: str
    title: str
    kind: str
    status: str
    success_criteria: list[str]
    protects: list[str]
    always_run: bool
    command: str | None = None
    working_directory: str | None = None
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class ValidationRunReport:
    """The aggregate results for a validation-contract run."""

    results: list[ValidationCheckResult]

    @property
    def passed(self) -> bool:
        """Return whether every executed command validation passed."""
        return all(result.status != "failed" for result in self.results)

    @property
    def has_pending_manual_validations(self) -> bool:
        """Return whether any checklist or manual validations remain pending."""
        return any(result.status == "pending" for result in self.results)


def run_validation_contract(contract: dict[str, Any], *, workspace: Path) -> ValidationRunReport:
    """Run enabled validations from *contract* against *workspace*."""
    validations = contract.get("validations", [])
    if not isinstance(validations, list):
        raise ValueError("Validation contract must contain a 'validations' array.")

    results: list[ValidationCheckResult] = []
    for index, entry in enumerate(validations):
        if not isinstance(entry, dict):
            raise ValueError(f"validation_contract.validations[{index}] must be an object.")
        if not entry.get("enabled", True):
            continue

        kind = entry.get("kind")
        if kind == "command":
            results.append(_run_command_validation(entry, workspace))
            continue
        if kind in {"checklist", "manual_gate"}:
            results.append(_pending_validation_result(entry))
            continue
        raise ValueError(f"Unsupported validation kind: {kind!r}")

    return ValidationRunReport(results=results)


def render_validation_report_markdown(report: ValidationRunReport, *, report_title: str) -> str:
    """Render a Markdown report for a validation run."""
    passed_count = sum(1 for result in report.results if result.status == "passed")
    failed_count = sum(1 for result in report.results if result.status == "failed")
    pending_count = sum(1 for result in report.results if result.status == "pending")
    lines = [
        f"# Validation Report: {report_title}",
        "",
        "## Summary",
        f"- **Passed:** {passed_count}",
        f"- **Failed:** {failed_count}",
        f"- **Pending Manual Checks:** {pending_count}",
        "",
        "## Results",
    ]

    if not report.results:
        lines.extend(["- No enabled validations were available.", ""])
        return "\n".join(lines)

    for result in report.results:
        lines.extend(
            [
                f"### {result.title} (`{result.validation_id}`)",
                f"- **Kind:** {result.kind}",
                f"- **Status:** {result.status}",
                f"- **Always Run:** {result.always_run}",
            ]
        )
        if result.working_directory:
            lines.append(f"- **Working Directory:** {result.working_directory}")
        if result.command:
            lines.append(f"- **Command:** `{result.command}`")
        if result.exit_code is not None:
            lines.append(f"- **Exit Code:** {result.exit_code}")
        lines.append("- **Success Criteria:**")
        lines.extend(f"  - {item}" for item in result.success_criteria)
        lines.append("- **Protects:**")
        lines.extend(f"  - {item}" for item in result.protects)
        if result.stdout:
            lines.extend(["", "#### STDOUT", "", "```text", result.stdout.rstrip(), "```"])
        if result.stderr:
            lines.extend(["", "#### STDERR", "", "```text", result.stderr.rstrip(), "```"])
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _run_command_validation(entry: dict[str, Any], workspace: Path) -> ValidationCheckResult:
    """Execute one command validation entry."""
    command = _required_string(entry, "command")
    working_directory = _required_string(entry, "working_directory")
    cwd = _resolve_working_directory(workspace, working_directory)
    logger.debug("Running validation command %s in %s", command, cwd)

    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            shell=True,
            capture_output=True,
            text=True,
            check=False,
        )
        status = "passed" if result.returncode == 0 else "failed"
        return ValidationCheckResult(
            validation_id=_required_string(entry, "id"),
            title=_required_string(entry, "title"),
            kind=_required_string(entry, "kind"),
            status=status,
            success_criteria=_string_list(entry, "success_criteria"),
            protects=_string_list(entry, "protects"),
            always_run=bool(entry.get("always_run", False)),
            command=command,
            working_directory=working_directory,
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
    except OSError as exc:
        return ValidationCheckResult(
            validation_id=_required_string(entry, "id"),
            title=_required_string(entry, "title"),
            kind=_required_string(entry, "kind"),
            status="failed",
            success_criteria=_string_list(entry, "success_criteria"),
            protects=_string_list(entry, "protects"),
            always_run=bool(entry.get("always_run", False)),
            command=command,
            working_directory=working_directory,
            stderr=str(exc),
        )


def _pending_validation_result(entry: dict[str, Any]) -> ValidationCheckResult:
    """Return a pending result for non-command validations."""
    return ValidationCheckResult(
        validation_id=_required_string(entry, "id"),
        title=_required_string(entry, "title"),
        kind=_required_string(entry, "kind"),
        status="pending",
        success_criteria=_string_list(entry, "success_criteria"),
        protects=_string_list(entry, "protects"),
        always_run=bool(entry.get("always_run", False)),
    )


def _required_string(entry: dict[str, Any], key: str) -> str:
    """Return a required string field from *entry*."""
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Validation entry field '{key}' must be a non-empty string.")
    return value


def _string_list(entry: dict[str, Any], key: str) -> list[str]:
    """Return a string-list field from *entry*."""
    value = entry.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"Validation entry field '{key}' must be a list of strings.")
    return list(value)


def _resolve_working_directory(workspace: Path, working_directory: str) -> Path:
    """Resolve a validation working directory relative to *workspace*."""
    path = Path(working_directory)
    return path if path.is_absolute() else workspace / path
