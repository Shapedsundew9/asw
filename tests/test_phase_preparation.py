"""Tests for phase-preparation helpers."""

from __future__ import annotations

from pathlib import Path

from asw.phase_preparation import (
    build_phase_artifact_paths,
    find_tracked_file_mutations,
    lint_devops_proposal,
    lint_phase_design,
    render_setup_summary,
)

_VALID_PHASE_DESIGN = """\
# Phase Design: Local Validation

## Phase Summary
- Keep the phase limited to local validation.

## Task Mapping
```json
{
  "tasks": [
    {
      "id": "prepare_environment",
      "title": "Prepare environment",
      "owner": "DevOps Engineer",
      "objective": "Provision the local toolchain.",
      "depends_on": [],
      "deliverables": ["Setup proposal"],
      "acceptance_criteria": ["Tooling is available locally"]
    }
  ]
}
```

## Required Tooling
- pytest

## Sequencing Notes
- Environment preparation happens before implementation.
"""

_VALID_DEVOPS_PROPOSAL = """\
# DevOps Setup Proposal: Local Validation

## Execution Summary
Prepare the local environment without modifying tracked repository files.

## Safety Notes
- The script stays inside the local workspace.
- The script does not invoke git.

## Repo Impact
- Will create or update the local `.venv` environment.
- Will not edit tracked repository files.

## Setup Script
```bash
#!/usr/bin/env bash
set -euo pipefail
trap 'echo "failed" >&2' ERR

if [[ ! -d ".venv" ]]; then
  python3 -m venv .venv
fi
```
"""


def test_build_phase_artifact_paths_uses_stable_names(tmp_path: Path) -> None:
    """Artifact paths should use stable per-phase filenames."""
    company = tmp_path / ".company"
    paths = build_phase_artifact_paths(company, 0)

    assert paths.draft_path.name == "01_design_draft.md"
    assert paths.final_path.name == "01_design_final.md"
    assert paths.proposal_path.name == "01_setup_proposal.md"
    assert paths.summary_path.name == "01_setup_summary.md"
    assert paths.script_path == tmp_path / ".devcontainer" / "phase_01_setup.sh"


def test_lint_phase_design_accepts_valid_output() -> None:
    """A valid phase design should pass with the approved role set."""
    errors, task_mapping = lint_phase_design(
        _VALID_PHASE_DESIGN,
        allowed_roles={"Development Lead", "DevOps Engineer", "Python Backend Developer"},
    )

    assert not errors
    assert task_mapping is not None
    assert '"prepare_environment"' in task_mapping


def test_lint_phase_design_rejects_missing_tooling_section() -> None:
    """Phase designs must include a tooling section with a Markdown list."""
    invalid_design = _VALID_PHASE_DESIGN.replace("## Required Tooling\n- pytest\n\n", "")

    errors, _ = lint_phase_design(
        invalid_design,
        allowed_roles={"Development Lead", "DevOps Engineer", "Python Backend Developer"},
    )

    assert any("Required Tooling" in error for error in errors)


def test_lint_devops_proposal_rejects_git_commands() -> None:
    """Guarded DevOps proposals must reject repository git commands."""
    invalid_proposal = _VALID_DEVOPS_PROPOSAL.replace(
        'if [[ ! -d ".venv" ]]; then\n  python3 -m venv .venv\nfi\n',
        "git status\n",
    )

    errors, _ = lint_devops_proposal(invalid_proposal)

    assert any("git commands" in error for error in errors)


def test_render_setup_summary_references_generated_script_path(tmp_path: Path) -> None:
    """The rendered setup summary should point reviewers at the generated script."""
    script_path = tmp_path / ".devcontainer" / "phase_01_setup.sh"

    summary = render_setup_summary(_VALID_DEVOPS_PROPOSAL, script_path)

    assert str(script_path) in summary
    assert "Generated Script Artifact" in summary


def test_find_tracked_file_mutations_detects_hash_changes() -> None:
    """Tracked-file mutation detection should report changed paths."""
    before = {"src/asw/orchestrator.py": "old", "README.md": "same"}
    after = {"src/asw/orchestrator.py": "new", "README.md": "same"}

    changed = find_tracked_file_mutations(before, after)

    assert changed == ["src/asw/orchestrator.py"]
