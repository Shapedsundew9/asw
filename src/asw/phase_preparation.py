"""Helpers for phase design artifacts and guarded DevOps setup."""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from asw.company import hash_file
from asw.git import GitError, is_git_repo, repo_root
from asw.linters.json_lint import validate_phase_task_mapping
from asw.linters.markdown import (
    extract_markdown_section_body,
    validate_markdown_list_section,
    validate_sections,
)

logger = logging.getLogger("asw.phase_preparation")

_PHASE_DESIGN_REQUIRED_SECTIONS = [
    "Phase Summary",
    "Task Mapping",
    "Required Tooling",
    "Sequencing Notes",
]
_PHASE_FEEDBACK_REQUIRED_SECTIONS = [
    "Assessment",
    "Dependencies",
    "Tooling Needs",
    "Risks",
]
_DEVOPS_PROPOSAL_REQUIRED_SECTIONS = [
    "Execution Summary",
    "Safety Notes",
    "Repo Impact",
    "Setup Script",
]
_DANGEROUS_SCRIPT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(^|\n)\s*git\s+", re.IGNORECASE),
        "Setup scripts must not invoke git commands inside the self-hosted repository.",
    ),
    (
        re.compile(
            r"rm\s+-rf\s+(?:\$PWD|\./|/|/workspaces/|\.git\b|\.company\b|src\b|docs\b|tests\b)",
            re.IGNORECASE,
        ),
        "Setup scripts must not delete repository-controlled paths.",
    ),
    (
        re.compile(r"\b(?:sed|perl)\s+-i\b", re.IGNORECASE),
        "Setup scripts must not edit repository files in place.",
    ),
    (
        re.compile(
            r"(?:>|>>|tee\s+)(?:\s|\\n)*(?:src/|docs/|tests/|README\.md|requirements\.txt|"
            r"pyproject\.toml|\.devcontainer/(?!phase_\d+_setup\.sh))",
            re.IGNORECASE,
        ),
        "Setup scripts must not write into tracked repository files.",
    ),
    (
        re.compile(
            r"\b(?:mv|cp)\b.*\s(?:src/|docs/|tests/|README\.md|requirements\.txt|pyproject\.toml)",
            re.IGNORECASE,
        ),
        "Setup scripts must not copy or move files into tracked repository paths.",
    ),
    (
        re.compile(r"apply_patch", re.IGNORECASE),
        "Setup scripts must not attempt to patch repository files.",
    ),
)


@dataclass(frozen=True)
class PhaseArtifactPaths:
    """Resolved artifact paths for a single execution-plan phase."""

    stem: str
    artifacts_dir: Path
    draft_path: Path
    final_path: Path
    proposal_path: Path
    summary_path: Path
    script_path: Path

    def feedback_path(self, role_title: str) -> Path:
        """Return the feedback artifact path for *role_title*."""
        return self.artifacts_dir / f"{self.stem}_feedback_{_slugify(role_title)}.md"

    def attempt_log_path(self, attempt: int) -> Path:
        """Return the execution-log path for *attempt*."""
        return self.artifacts_dir / f"{self.stem}_setup_attempt_{attempt}.log"


def _slugify(value: str) -> str:
    """Return a filesystem-safe slug for *value*."""
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or "item"


def build_phase_artifact_paths(company: Path, phase_index: int) -> PhaseArtifactPaths:
    """Return the artifact paths for the phase at *phase_index*."""
    stem = f"{phase_index + 1:02d}"
    artifacts_dir = company / "artifacts" / "phases"
    return PhaseArtifactPaths(
        stem=stem,
        artifacts_dir=artifacts_dir,
        draft_path=artifacts_dir / f"{stem}_design_draft.md",
        final_path=artifacts_dir / f"{stem}_design_final.md",
        proposal_path=artifacts_dir / f"{stem}_setup_proposal.md",
        summary_path=artifacts_dir / f"{stem}_setup_summary.md",
        script_path=company.parent / ".devcontainer" / f"phase_{stem}_setup.sh",
    )


def extract_fenced_code_block(content: str, language: str) -> str | None:
    """Extract the first fenced *language* block from *content*."""
    pattern = rf"```{re.escape(language)}\s*(.*?)\s*```"
    match = re.search(pattern, content, re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else None


def extract_markdown_list_items(content: str, heading: str) -> list[str]:
    """Return Markdown list items from *heading* within *content*."""
    body = extract_markdown_section_body(content, heading)
    if body is None:
        return []

    items: list[str] = []
    for line in body.splitlines():
        match = re.match(r"^\s*[\-*+]\s+(.*?)\s*$", line)
        if match:
            items.append(match.group(1).strip())
    return items


def lint_phase_design(content: str, *, allowed_roles: set[str]) -> tuple[list[str], str | None]:
    """Validate a Development Lead phase-design artifact."""
    errors: list[str] = []
    errors.extend(validate_sections(content, _PHASE_DESIGN_REQUIRED_SECTIONS))
    errors.extend(validate_markdown_list_section(content, "Required Tooling"))

    if not re.match(r"^#\s+Phase Design:", content):
        errors.append("Phase design must start with a '# Phase Design:' heading.")

    json_block = extract_fenced_code_block(content, "json")
    if json_block is None:
        errors.append("No fenced ```json``` task-mapping block found in phase design output.")
    else:
        errors.extend(validate_phase_task_mapping(json_block, allowed_roles=allowed_roles))

    return errors, json_block


def lint_phase_feedback(content: str) -> list[str]:
    """Validate a per-role feedback artifact."""
    errors: list[str] = []
    if not re.match(r"^#\s+Phase Feedback:", content):
        errors.append("Phase feedback must start with a '# Phase Feedback:' heading.")

    errors.extend(validate_sections(content, _PHASE_FEEDBACK_REQUIRED_SECTIONS))
    for heading in _PHASE_FEEDBACK_REQUIRED_SECTIONS:
        errors.extend(validate_markdown_list_section(content, heading))

    return errors


def validate_setup_script_safety(script: str) -> list[str]:
    """Return safety violations for a generated DevOps setup script."""
    errors: list[str] = []
    stripped = script.lstrip()
    if not (stripped.startswith("#!/usr/bin/env bash") or stripped.startswith("#!/bin/bash")):
        errors.append("Setup script must start with a bash shebang.")

    if "set -euo pipefail" not in script:
        errors.append("Setup script must enable 'set -euo pipefail'.")

    for pattern, message in _DANGEROUS_SCRIPT_PATTERNS:
        if pattern.search(script):
            errors.append(message)

    return errors


def lint_devops_proposal(content: str) -> tuple[list[str], str | None]:
    """Validate a DevOps setup proposal and return its setup script."""
    errors: list[str] = []
    errors.extend(validate_sections(content, _DEVOPS_PROPOSAL_REQUIRED_SECTIONS))
    errors.extend(validate_markdown_list_section(content, "Safety Notes"))
    errors.extend(validate_markdown_list_section(content, "Repo Impact"))

    if not re.match(r"^#\s+DevOps Setup Proposal:", content):
        errors.append("DevOps proposal must start with a '# DevOps Setup Proposal:' heading.")

    script = extract_fenced_code_block(content, "bash")
    if script is None:
        errors.append("No fenced ```bash``` setup script found in DevOps proposal output.")
    else:
        errors.extend(validate_setup_script_safety(script))

    return errors, script


def render_setup_summary(proposal_content: str, script_path: Path) -> str:
    """Render a human-readable summary from a full setup proposal."""
    summary = re.sub(
        r"```bash\s*.*?```",
        f"- Generated script path: `{script_path}`",
        proposal_content,
        flags=re.DOTALL | re.IGNORECASE,
    )
    return summary.replace("## Setup Script", "## Generated Script Artifact", 1)


def snapshot_tracked_repo_files(workdir: Path) -> dict[str, str | None]:
    """Return hashes for all tracked files in the repository containing *workdir*."""
    if not is_git_repo(workdir):
        logger.debug("Skipping tracked-file snapshot because %s is not inside a git repo", workdir)
        return {}

    root = repo_root(workdir)
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        msg = result.stderr.strip() or "git ls-files failed"
        raise GitError(msg)

    snapshot: dict[str, str | None] = {}
    for rel_path in result.stdout.splitlines():
        path = root / rel_path
        snapshot[rel_path] = hash_file(path) if path.is_file() else None

    return snapshot


def find_tracked_file_mutations(
    before: dict[str, str | None],
    after: dict[str, str | None],
    *,
    allowed_paths: set[str] | None = None,
) -> list[str]:
    """Return tracked files whose hashes changed outside *allowed_paths*."""
    allowed = {path.replace("\\", "/") for path in (allowed_paths or set())}
    changed: list[str] = []

    for path in sorted(set(before) | set(after)):
        if path in allowed:
            continue
        if before.get(path) != after.get(path):
            changed.append(path)

    return changed
