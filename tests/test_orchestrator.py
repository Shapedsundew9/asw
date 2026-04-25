"""Integration test for the orchestrator pipeline."""

# pylint: disable=too-many-lines

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import asw.orchestrator as orchestrator_module
from asw.company import init_company, new_pipeline_state, read_pipeline_state, snapshot_paths, write_pipeline_state
from asw.gates import ExecutionApprovalResult, FounderReviewResult
from asw.git import GitError
from asw.llm.errors import LLMInvocationError, TransientLLMError
from asw.orchestrator import (
    PipelineRunOptions,
    _agent_loop,
    _init_pipeline_state,
    _is_phase_done,
    _phase_design_input_paths,
    _render_architecture_markdown,
    _run_or_skip_phase_design_step,
    _run_phase_design_step,
    _run_phase_implementation_loop,
    run_pipeline,
)
from asw.phase_preparation import PhaseArtifactPaths, build_phase_artifact_paths
from asw.phase_tasks import write_phase_task_mapping
from asw.pipeline import PipelineExecutionContext
from asw.validation_contract import ensure_validation_contract, validation_contract_paths
from asw.validation_runner import ValidationCheckResult, ValidationRunReport

# ── Unit tests ──────────────────────────────────────────────────────────


def test_render_architecture_markdown_string_lists() -> None:
    """Ensure we safely handle single strings instead of lists in architecture."""
    data = {
        "project_name": "Test",
        "tech_stack": {
            "frameworks": "React",
            "tools": "Webpack",
        },
        "components": [
            {"name": "Frontend", "interfaces": "HTTP"},
        ],
        "deployment": {
            "requirements": "Modern browser",
        },
    }
    json_str = json.dumps(data)
    md = _render_architecture_markdown(json_str, "graph TD\nA-->B")

    assert "- **Frameworks:** React" in md
    assert "- **Tools:** Webpack" in md
    assert "| Frontend | N/A | HTTP |" in md
    assert "- **Requirements:** Modern browser" in md
    assert "M, o, d, e, r, n" not in md


def test_init_pipeline_state_bootstraps_validation_contract(tmp_path: Path) -> None:
    """Pipeline initialization should create the validation contract artifacts once."""
    state, company = _init_pipeline_state(tmp_path)

    contract_json_path, contract_md_path = validation_contract_paths(company)

    assert state == read_pipeline_state(tmp_path)
    assert contract_json_path.is_file()
    assert contract_md_path.is_file()

    first_json = contract_json_path.read_text(encoding="utf-8")
    first_markdown = contract_md_path.read_text(encoding="utf-8")

    second_state, second_company = _init_pipeline_state(tmp_path)

    assert second_company == company
    assert second_state == state
    assert contract_json_path.read_text(encoding="utf-8") == first_json
    assert contract_md_path.read_text(encoding="utf-8") == first_markdown


def test_agent_loop_retries_only_transient_backend_failures() -> None:
    """Transient backend failures should be retried."""
    agent = MagicMock()
    agent.name = "CPO"
    agent.run = MagicMock(side_effect=[TransientLLMError("busy", reason="busy"), "valid output"])

    result = _agent_loop(agent, {"vision": "demo"}, lambda _: [], "PRD")

    assert result == "valid output"
    assert agent.run.call_count == 2


def test_agent_loop_uses_role_aware_status_without_first_attempt_label(capsys: pytest.CaptureFixture[str]) -> None:
    """The initial agent run should show concise status text without attempt-one noise."""
    agent = MagicMock()
    agent.name = "CPO"
    agent.run = MagicMock(return_value="valid output")

    with patch("asw.orchestrator._supports_live_status", return_value=False):
        result = _agent_loop(agent, {"vision": "demo"}, lambda _: [], "PRD")

    captured = capsys.readouterr()
    assert result == "valid output"
    assert "attempt 1" not in captured.out.lower()
    assert "may take up to 5 min" not in captured.out.lower()
    assert "via gemini cli" not in captured.out.lower()
    assert "CPO drafting the PRD" in captured.out


def test_agent_loop_shows_feedback_stage_status(capsys: pytest.CaptureFixture[str]) -> None:
    """Feedback runs should describe the review work rather than the raw agent label."""
    agent = MagicMock()
    agent.name = "DevOps Engineer Feedback"
    agent.run = MagicMock(return_value="valid output")

    with patch("asw.orchestrator._supports_live_status", return_value=False):
        _agent_loop(
            agent,
            {"phase_design_draft": "demo"},
            lambda _: [],
            "phase_1 - Local Validation Feedback: DevOps Engineer",
        )

    captured = capsys.readouterr()
    assert "DevOps Engineer reviewing the design for phase_1 - Local Validation" in captured.out


def test_agent_loop_shows_retry_label_only_when_retrying(capsys: pytest.CaptureFixture[str]) -> None:
    """Retry output should appear only after a transient failure triggers another attempt."""
    agent = MagicMock()
    agent.name = "CPO"
    agent.run = MagicMock(side_effect=[TransientLLMError("busy", reason="busy"), "valid output"])

    with patch("asw.orchestrator._supports_live_status", return_value=False):
        _agent_loop(agent, {"vision": "demo"}, lambda _: [], "PRD")

    captured = capsys.readouterr()
    assert "attempt 1" not in captured.out.lower()
    assert "Retry 2/3: CPO drafting the PRD" in captured.out


def test_agent_loop_fails_fast_on_non_retryable_backend_error() -> None:
    """Non-transient backend failures should not be retried."""
    agent = MagicMock()
    agent.name = "CPO"
    agent.run = MagicMock(side_effect=LLMInvocationError("bad request", reason="non-transient-cli-error"))

    with (
        patch("asw.orchestrator.sys.exit", side_effect=SystemExit(1)) as mock_exit,
        pytest.raises(SystemExit),
    ):
        _agent_loop(agent, {"vision": "demo"}, lambda _: [], "PRD")

    mock_exit.assert_called_once_with(1)
    assert agent.run.call_count == 1


def test_agent_loop_fails_fast_on_lint_errors() -> None:
    """Mechanically invalid output should not be sent back for another LLM attempt."""
    agent = MagicMock()
    agent.name = "CPO"
    agent.run = MagicMock(return_value="invalid output")

    with (
        patch("asw.orchestrator.sys.exit", side_effect=SystemExit(1)) as mock_exit,
        pytest.raises(SystemExit),
    ):
        _agent_loop(agent, {"vision": "demo"}, lambda _: ["Missing section"], "PRD")

    mock_exit.assert_called_once_with(1)
    assert agent.run.call_count == 1


def test_agent_loop_fails_fast_on_lint_errors_and_saves_failed_artifact(tmp_path: Path) -> None:
    """Lint failures should be written to .company/artifacts/failed/ for inspection."""
    agent = MagicMock()
    agent.name = "CPO"
    agent.run = MagicMock(return_value="invalid output")
    company = init_company(tmp_path)
    agent.role_file = company / "roles" / "cpo.md"

    with (
        patch("asw.orchestrator.sys.exit", side_effect=SystemExit(1)) as mock_exit,
        pytest.raises(SystemExit),
    ):
        _agent_loop(agent, {"vision": "demo"}, lambda _: ["Missing section"], "PRD")

    mock_exit.assert_called_once_with(1)
    failed_files = list((company / "artifacts" / "failed").glob("prd_attempt1_*.md"))
    assert len(failed_files) == 1
    content = failed_files[0].read_text(encoding="utf-8")
    assert "Missing section" in content
    assert "invalid output" in content


# ── Canned LLM responses ────────────────────────────────────────────────

_CANNED_PRD = """\
## Executive Summary

AgenticOrg CLI orchestrates LLM-based agents.

## Goals & Success Metrics

- Automate SDLC phases.

## Target Users

Solo founders.

## Functional Requirements

- CLI start command.

## Non-Functional Requirements

- Must run inside DevContainer.

## User Stories

- As a founder, I want to run a single command, so that agents produce a PRD.

## Acceptance Criteria Checklist

- [x] CLI accepts --vision flag
- [x] PRD is generated
- [x] Architecture JSON is generated
- [x] Founder review gate pauses pipeline

## System Overview Diagram

```mermaid
graph TD
    A[Founder] --> B[CLI]
    B --> C[CPO Agent]
    B --> D[CTO Agent]
```

## Risks & Mitigations

- LLM hallucination → mechanical linting.

## Open Questions

- None.
"""

_CANNED_ARCH = """\
```json
{
    "project_name": "agenticorg",
    "tech_stack": {"language": "Python", "version": "3.14", "frameworks": [], "tools": ["argparse"]},
    "components": [{"name": "CLI", "responsibility": "Entry point", "interfaces": ["start"]}],
    "data_models": [{"name": "Vision", "fields": [{"name": "content", "type": "str"}]}],
    "api_contracts": [{"endpoint": "/start", "method": "CLI", "description": "Start pipeline"}],
    "deployment": {"platform": "DevContainer", "strategy": "local", "requirements": ["Docker"]}
}
```

```mermaid
graph TD
    CLI --> Orchestrator
    Orchestrator --> CPO
    Orchestrator --> CTO
```
"""

_CANNED_EXECUTION_PLAN = """\
```json
{
    "phases": [
        {
            "id": "phase_1",
            "name": "Local Validation",
            "objective": "Validate the core workflow locally before production hardening.",
            "scope": "Use local-only infrastructure and defer hosted operations.",
            "deliverables": ["Core flow works locally", "Acceptance checks exist"],
            "exit_criteria": ["Founder can run the product locally", "Core checks pass"],
            "selected_team_roles": ["Development Lead", "DevOps Engineer", "Python Backend Developer"]
        }
    ],
    "selected_team": [
        {
            "title": "Development Lead",
            "filename": "development_lead.md",
            "responsibility": "Coordinate phase design, task ownership, and implementation review.",
            "rationale": "This role is immediately needed to turn the approved plan into executable delivery structure."
        },
        {
            "title": "DevOps Engineer",
            "filename": "devops_engineer.md",
            "responsibility": "Prepare the delivery environment and required tooling.",
            "rationale": "This role is immediately needed to keep tooling and environment setup repeatable."
        },
        {
            "title": "Python Backend Developer",
            "filename": "python_backend_developer.md",
            "responsibility": "Implement CLI entry point and orchestrator logic.",
            "rationale": "This role is immediately needed to build the first validated product slice."
        }
    ],
    "generic_role_catalog": [
        {
            "title": "Documentation Standards Lead",
            "summary": "Own tutorials, training material, and documentation quality control.",
            "when_needed": "Needed once the product surface evolves faster than the docs stay current."
        }
    ],
    "deferred_roles_or_capabilities": [
        {
            "name": "Hosted Operations Platform",
            "rationale": "Deferred until the product needs hosted infrastructure, monitoring, and persistent storage."
        }
    ]
}
```
"""

_CANNED_ROSTER = """\
```json
{
    "hired_agents": [
        {
            "title": "Development Lead",
            "filename": "development_lead.md",
            "responsibility": "Coordinate phase design, task ownership, and implementation review.",
            "mission": "Turn the approved execution plan into clear team delivery structure.",
            "scope": "Own design harmonisation, task sequencing, and delta review for the approved phase.",
            "key_deliverables": [
                "Publish phase design artifacts",
                "Review implementation deltas"
            ],
            "collaborators": ["Founder", "DevOps Engineer"],
            "assigned_standards": []
        },
        {
            "title": "DevOps Engineer",
            "filename": "devops_engineer.md",
            "responsibility": "Prepare the delivery environment and required tooling.",
            "mission": "Make the approved implementation phase executable inside the project environment.",
            "scope": "Own environment setup, tooling installation, and operational guardrails for the phase.",
            "key_deliverables": [
                "Prepare setup steps",
                "Document environment changes"
            ],
            "collaborators": ["Development Lead", "Founder"],
            "assigned_standards": []
        },
        {
            "title": "Python Backend Developer",
            "filename": "python_backend_developer.md",
            "responsibility": "Implement CLI entry point and orchestrator logic.",
            "mission": "Deliver the first validated backend workflow for Phase 1.",
            "scope": "Own the CLI entry point, orchestration flow, and first persistence path for local validation.",
            "key_deliverables": [
                "Implement the first CLI workflow",
                "Write backend and orchestration tests"
            ],
            "collaborators": ["Founder", "Documentation Standards Lead"],
            "assigned_standards": ["python_guidelines.md"]
        }
    ]
}
```
"""

_CANNED_ROLE = """\
# Role: Python Backend Developer

You are the **Python Backend Developer** of an elite software engineering company. You implement the CLI entry point and orchestrator logic for the agenticorg project.

## Context

You receive architecture specifications and implement Python backend services including the CLI module and pipeline orchestration.

## Output Format

Produce Python source files following PEP 8 with full type annotations and Google-style docstrings. Each file must include comprehensive unit tests.

## Strict Rules

- Follow PEP 8 and use type annotations on all function signatures.
- Use Google-style docstrings for all public modules, classes, and functions.
- Prefer Python standard library modules over third-party packages.
- Do NOT include any text outside of the code blocks. No preamble, no sign-off.
- Under NO circumstances omit type annotations or docstrings.
"""

_CANNED_PHASE_DESIGN_DRAFT = """\
# Phase Design: Local Validation

## Phase Summary
- Deliver the first validated local workflow without expanding into hosted operations.

## Task Mapping
```json
{
    "tasks": [
        {
            "id": "prepare_environment",
            "title": "Prepare the local validation environment",
            "owner": "DevOps Engineer",
            "objective": "Make the validated local toolchain available inside the repository workspace.",
            "depends_on": [],
            "deliverables": ["Guarded setup proposal and script"],
            "acceptance_criteria": ["Required tooling is available without tracked repo mutations"]
        },
        {
            "id": "implement_workflow",
            "title": "Implement the local validation workflow",
            "owner": "Python Backend Developer",
            "objective": "Build the first validated local path for the approved scope.",
            "depends_on": ["prepare_environment"],
            "deliverables": ["Workflow code changes", "Backend tests"],
            "acceptance_criteria": ["Founder can run the local workflow end to end"]
        },
        {
            "id": "coordinate_readiness",
            "title": "Coordinate implementation readiness",
            "owner": "Development Lead",
            "objective": "Keep the phase sequence, ownership, and completion boundaries explicit.",
            "depends_on": ["prepare_environment", "implement_workflow"],
            "deliverables": ["Harmonized phase design"],
            "acceptance_criteria": ["Task ownership and sequencing are explicit for the full phase"]
        }
    ]
}
```

## Required Tooling
- Python virtual environment
- pytest

## Sequencing Notes
- DevOps Engineer prepares the environment before implementation starts.
- Python Backend Developer implements the approved local workflow after environment prep.
- Development Lead keeps sequencing and acceptance boundaries aligned across the phase.
"""

_CANNED_PHASE_FEEDBACK_DEVELOPMENT_LEAD = """\
# Phase Feedback: Development Lead

## Assessment
- The draft keeps the phase limited to local validation and assigns clear ownership.

## Dependencies
- None.

## Tooling Needs
- None.

## Risks
- Keep environment preparation ahead of implementation so later tasks do not start on incomplete tooling.
"""

_CANNED_PHASE_FEEDBACK_DEVOPS = """\
# Phase Feedback: DevOps Engineer

## Assessment
- The draft gives DevOps a clear first task and keeps the environment scope narrow.

## Dependencies
- None.

## Tooling Needs
- Python virtual environment
- pytest

## Risks
- The setup script should avoid mutating tracked repository files in the self-hosted repo.
"""

_CANNED_PHASE_FEEDBACK_BACKEND = """\
# Phase Feedback: Python Backend Developer

## Assessment
- The draft gives the backend role a concrete implementation target and a clear dependency on environment prep.

## Dependencies
- Environment preparation must finish before implementation begins.

## Tooling Needs
- pytest

## Risks
- The backend task should stay focused on the approved local workflow rather than deferred platform work.
"""

_CANNED_PHASE_DESIGN_FINAL = """\
# Phase Design: Local Validation

## Phase Summary
- Deliver the first validated local workflow with explicit ownership, safe environment prep, and no hosted-scope expansion.

## Task Mapping
```json
{
    "tasks": [
        {
            "id": "prepare_environment",
            "title": "Prepare the local validation environment",
            "owner": "DevOps Engineer",
            "objective": "Make the validated local toolchain available inside the repository workspace.",
            "depends_on": [],
            "deliverables": ["Guarded setup proposal and script"],
            "acceptance_criteria": ["Required tooling is available without tracked repo mutations"]
        },
        {
            "id": "implement_workflow",
            "title": "Implement the local validation workflow",
            "owner": "Python Backend Developer",
            "objective": "Build the first validated local path for the approved scope.",
            "depends_on": ["prepare_environment"],
            "deliverables": ["Workflow code changes", "Backend tests"],
            "acceptance_criteria": ["Founder can run the local workflow end to end"]
        },
        {
            "id": "coordinate_readiness",
            "title": "Coordinate implementation readiness",
            "owner": "Development Lead",
            "objective": "Keep the phase sequence, ownership, and completion boundaries explicit.",
            "depends_on": ["prepare_environment", "implement_workflow"],
            "deliverables": ["Harmonized phase design"],
            "acceptance_criteria": ["Task ownership and sequencing are explicit for the full phase"]
        }
    ]
}
```

## Required Tooling
- Python virtual environment
- pytest

## Sequencing Notes
- Environment preparation is Founder-gated and must complete before implementation begins.
- Backend implementation stays within the approved local-validation boundary.
- Development Lead maintains the approved sequence and acceptance boundaries.
"""

_CANNED_DEVOPS_PROPOSAL = """\
# DevOps Setup Proposal: Local Validation

## Execution Summary
Prepare the local validation environment by ensuring the repository virtual environment exists and repository Python dependencies are installed without changing tracked source files.

## Safety Notes
- The script only creates or updates the local `.venv` environment.
- The script does not invoke git or overwrite tracked repository files.

## Repo Impact
- Will create or update the local virtual environment and install Python dependencies into it.
- Will not modify tracked files under `src/`, `docs/`, `tests/`, or existing `.devcontainer` bootstrap files.

## Setup Script
```bash
#!/usr/bin/env bash
set -euo pipefail
trap 'echo "DevOps phase setup failed at line $LINENO while running: $BASH_COMMAND" >&2' ERR

if [[ ! -d ".venv" ]]; then
    python3 -m venv .venv
fi

.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```
"""

_APPROVE_REVIEW = FounderReviewResult(action="approve")
_APPROVE_EXECUTION = ExecutionApprovalResult(action="approve")
_SUCCESSFUL_DEVOPS_EXECUTION = subprocess.CompletedProcess(
    args=["bash", ".devcontainer/phase_01_setup.sh"],
    returncode=0,
    stdout="setup ok\n",
    stderr="",
)


def _phase_preparation_responses() -> list[str]:
    """Return canned LLM responses for the phase-preparation slice."""
    return [
        _CANNED_PHASE_DESIGN_DRAFT,
        _CANNED_PHASE_FEEDBACK_DEVELOPMENT_LEAD,
        _CANNED_PHASE_FEEDBACK_DEVOPS,
        _CANNED_PHASE_FEEDBACK_BACKEND,
        _CANNED_PHASE_DESIGN_FINAL,
        _CANNED_DEVOPS_PROPOSAL,
    ]


def _implementation_plan_responses() -> list[str]:
    """Return canned planning responses for the implementation loop."""
    return [
        (
            "# Implementation Plan: phase_1 - Local Validation Turn 1\n\n"
            "## Task Summary\n- Prepare the local environment.\n\n"
            "## Planned Changes\n- Apply the approved environment updates for the turn.\n\n"
            "## Validation Approach\n- Re-run the validation contract after execution.\n\n"
            "## Risks\n- None.\n"
        ),
        (
            "# Implementation Plan: phase_1 - Local Validation Turn 2\n\n"
            "## Task Summary\n- Implement the approved local workflow.\n\n"
            "## Planned Changes\n- Apply the approved backend workflow changes for the turn.\n\n"
            "## Validation Approach\n- Re-run the validation contract after execution.\n\n"
            "## Risks\n- None.\n"
        ),
        (
            "# Implementation Plan: phase_1 - Local Validation Turn 3\n\n"
            "## Task Summary\n- Coordinate implementation readiness.\n\n"
            "## Planned Changes\n- Update the readiness documentation for the turn.\n\n"
            "## Validation Approach\n- Re-run the validation contract after execution.\n\n"
            "## Risks\n- None.\n"
        ),
    ]


def _implementation_execute_responses() -> list[str]:
    """Return canned execution responses for the implementation loop."""
    return [
        (
            "# Implementation Execution: phase_1 - Local Validation Turn 1\n\n"
            "## Completed Work\n- Completed the environment task.\n\n"
            "## Files Changed\n- None.\n\n"
            "## Validation Notes\n- Validation contract will be re-run by the orchestrator.\n\n"
            "## Follow-Up\n- None.\n"
        ),
        (
            "# Implementation Execution: phase_1 - Local Validation Turn 2\n\n"
            "## Completed Work\n- Completed the workflow task.\n\n"
            "## Files Changed\n- None.\n\n"
            "## Validation Notes\n- Validation contract will be re-run by the orchestrator.\n\n"
            "## Follow-Up\n- None.\n"
        ),
        (
            "# Implementation Execution: phase_1 - Local Validation Turn 3\n\n"
            "## Completed Work\n- Completed the readiness task.\n\n"
            "## Files Changed\n- None.\n\n"
            "## Validation Notes\n- Validation contract will be re-run by the orchestrator.\n\n"
            "## Follow-Up\n- None.\n"
        ),
    ]


def _implementation_review_responses() -> list[str]:
    """Return canned approving review responses for the implementation loop."""
    response = """```json
{
  "decision": "approve",
  "summary": "The turn stayed in scope and the validation coverage remains adequate.",
  "scope_findings": [],
  "standards_findings": [],
  "validation_findings": [],
  "required_follow_up": []
}
```"""
    return [response, response, response]


def _configure_mock_llm(mock: MagicMock, *, invoke_responses: list[str]) -> MagicMock:
    """Attach invoke, plan, and execute side effects for the full pipeline."""
    mock.invoke = MagicMock(side_effect=invoke_responses)
    mock.invoke_plan = MagicMock(side_effect=_implementation_plan_responses())
    mock.invoke_execute = MagicMock(side_effect=_implementation_execute_responses())
    return mock


_CANNED_PRD_WITH_QUESTIONS = """\
## Executive Summary

AgenticOrg CLI orchestrates LLM-based agents.

## Goals & Success Metrics

- Automate SDLC phases.

## Target Users

Solo founders.

## Functional Requirements

- CLI start command.

## Non-Functional Requirements

- Must run inside DevContainer.

## User Stories

- As a founder, I want to run a single command, so that agents produce a PRD.

## Acceptance Criteria Checklist

- [x] CLI accepts --vision flag
- [x] PRD is generated

## System Overview Diagram

```mermaid
graph TD
    A[Founder] --> B[CLI]
    B --> C[CPO Agent]
```

## Risks & Mitigations

- LLM hallucination → mechanical linting.

## Open Questions

- Founder questions will be captured in the CLI review flow and persisted here after review.

```json
{
  "founder_questions": [
    {
      "question": "Which database should we use?",
      "choices": ["PostgreSQL", "SQLite"]
    }
  ]
}
```
"""

_CANNED_ARCH_WITH_QUESTIONS = """\
```json
{
    "project_name": "agenticorg",
    "tech_stack": {"language": "Python", "version": "3.14", "frameworks": [], "tools": ["argparse"]},
    "components": [{"name": "CLI", "responsibility": "Entry point", "interfaces": ["start"]}],
    "data_models": [{"name": "Vision", "fields": [{"name": "content", "type": "str"}]}],
    "api_contracts": [{"endpoint": "/start", "method": "CLI", "description": "Start pipeline"}],
    "deployment": {"platform": "DevContainer", "strategy": "local", "requirements": ["Docker"]},
    "founder_questions": [
        {"question": "Should we deploy locally first?", "choices": ["Yes", "No"]}
    ]
}
```

```mermaid
graph TD
    CLI --> Orchestrator
```
"""

_CANNED_EXECUTION_PLAN_WITH_QUESTIONS = """\
```json
{
    "phases": [
        {
            "id": "phase_1",
            "name": "Local Validation",
            "objective": "Validate the core workflow locally before production hardening.",
            "scope": "Use local-only infrastructure and defer hosted operations.",
            "deliverables": ["Core flow works locally"],
            "exit_criteria": ["Founder can run the product locally"],
            "selected_team_roles": ["Development Lead", "DevOps Engineer", "Python Backend Developer"]
        }
    ],
    "selected_team": [
        {
            "title": "Development Lead",
            "filename": "development_lead.md",
            "responsibility": "Coordinate phase design, task ownership, and implementation review.",
            "rationale": "This role is immediately needed to turn the approved plan into executable delivery structure."
        },
        {
            "title": "DevOps Engineer",
            "filename": "devops_engineer.md",
            "responsibility": "Prepare the delivery environment and required tooling.",
            "rationale": "This role is immediately needed to keep tooling and environment setup repeatable."
        },
        {
            "title": "Python Backend Developer",
            "filename": "python_backend_developer.md",
            "responsibility": "Implement CLI entry point and orchestrator logic.",
            "rationale": "This role is immediately needed to build the first validated product slice."
        }
    ],
    "generic_role_catalog": [
        {
            "title": "Documentation Standards Lead",
            "summary": "Own tutorials, training material, and documentation quality control.",
            "when_needed": "Needed once the product surface evolves faster than the docs stay current."
        }
    ],
    "deferred_roles_or_capabilities": [
        {
            "name": "Hosted Operations Platform",
            "rationale": "Deferred until the product needs hosted infrastructure, monitoring, and persistent storage."
        }
    ],
    "founder_questions": [
        {
            "question": "Should Phase 1 remain local-only?",
            "choices": ["Yes", "No"]
        }
    ]
}
```
"""


def _extract_json_block(content: str) -> str:
    """Extract the JSON body from a fenced JSON block."""
    return content.split("```json\n", maxsplit=1)[1].split("\n```", maxsplit=1)[0]


def _expected_role_output_paths(company: Path) -> list[Path]:
    """Return the role files expected from the canned roster."""
    return [
        company / "roles" / "development_lead.md",
        company / "roles" / "devops_engineer.md",
        company / "roles" / "python_backend_developer.md",
    ]


def _make_mock_llm() -> MagicMock:
    """Create a mock LLM backend that returns canned responses for all phases."""
    mock = MagicMock()
    return _configure_mock_llm(
        mock,
        invoke_responses=[
            _CANNED_PRD,
            _CANNED_ARCH,
            _CANNED_EXECUTION_PLAN,
            _CANNED_ROSTER,
            _CANNED_ROLE,
            *_phase_preparation_responses(),
            *_implementation_review_responses(),
        ],
    )


def _single_turn_execution_plan_json() -> str:
    """Return a minimal execution plan JSON string for a single implementation turn."""
    return json.dumps(
        {
            "phases": [
                {
                    "id": "phase_1",
                    "name": "Implementation Slice",
                    "selected_team_roles": ["Development Lead", "Python Backend Developer"],
                }
            ]
        }
    )


def _single_turn_roster_json() -> str:
    """Return a minimal roster JSON string for a single implementation turn."""
    return json.dumps(
        {
            "hired_agents": [
                {
                    "title": "Development Lead",
                    "filename": "development_lead.md",
                    "assigned_standards": [],
                },
                {
                    "title": "Python Backend Developer",
                    "filename": "python_backend_developer.md",
                    "assigned_standards": ["python_guidelines.md"],
                },
            ]
        }
    )


def _single_turn_task_mapping() -> dict[str, object]:
    """Return a minimal one-turn task mapping."""
    return {
        "tasks": [
            {
                "id": "implement_slice",
                "title": "Implement the approved slice",
                "owner": "Python Backend Developer",
                "objective": "Apply the approved implementation change for the current slice.",
                "depends_on": [],
                "deliverables": ["Code change"],
                "acceptance_criteria": ["Validation coverage remains adequate"],
            }
        ]
    }


def _write_single_turn_phase_artifacts(company: Path) -> PhaseArtifactPaths:
    """Write a minimal prepared phase artifact bundle for implementation-loop tests."""
    paths = build_phase_artifact_paths(company, 0)
    paths.final_path.parent.mkdir(parents=True, exist_ok=True)
    paths.final_path.write_text(
        "# Phase Design: Implementation Slice\n\n## Phase Summary\n- Deliver the current slice.\n",
        encoding="utf-8",
    )
    write_phase_task_mapping(_single_turn_task_mapping(), paths, phase_label="phase_1 - Implementation Slice")
    return paths


def _two_turn_task_mapping() -> dict[str, object]:
    """Return a minimal two-turn task mapping for invalidation tests."""
    return {
        "tasks": [
            {
                "id": "implement_first",
                "title": "Implement the first slice",
                "owner": "Python Backend Developer",
                "objective": "Apply the first implementation change.",
                "depends_on": [],
                "deliverables": ["Code change"],
                "acceptance_criteria": ["Validation remains adequate"],
            },
            {
                "id": "implement_second",
                "title": "Implement the second slice",
                "owner": "Python Backend Developer",
                "objective": "Apply the second implementation change.",
                "depends_on": ["implement_first"],
                "deliverables": ["Code change"],
                "acceptance_criteria": ["Validation remains adequate"],
            },
        ]
    }


def _write_two_turn_phase_artifacts(company: Path) -> PhaseArtifactPaths:
    """Write a minimal two-turn prepared phase artifact bundle."""
    paths = build_phase_artifact_paths(company, 0)
    paths.final_path.parent.mkdir(parents=True, exist_ok=True)
    paths.final_path.write_text(
        "# Phase Design: Implementation Slice\n\n## Phase Summary\n- Deliver the current slice.\n",
        encoding="utf-8",
    )
    write_phase_task_mapping(_two_turn_task_mapping(), paths, phase_label="phase_1 - Implementation Slice")
    return paths


def _make_single_turn_exec_ctx(
    tmp_path: Path,
    mock_llm: MagicMock,
    *,
    no_commit: bool,
) -> tuple[PipelineExecutionContext, str, str, str, PhaseArtifactPaths]:
    """Return a minimal execution context for direct implementation-loop tests."""
    _setup_git_repo(tmp_path)
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n\nBuild the implementation slice.\n", encoding="utf-8")

    company = init_company(tmp_path)
    ensure_validation_contract(company)
    (company / "roles" / "python_backend_developer.md").write_text(_CANNED_ROLE, encoding="utf-8")

    architecture_json = _extract_json_block(_CANNED_ARCH)
    execution_plan_json = _single_turn_execution_plan_json()
    roster_json = _single_turn_roster_json()
    (company / "artifacts" / "architecture.json").write_text(architecture_json, encoding="utf-8")
    (company / "artifacts" / "execution_plan.json").write_text(execution_plan_json, encoding="utf-8")
    (company / "artifacts" / "roster.json").write_text(roster_json, encoding="utf-8")

    paths = _write_single_turn_phase_artifacts(company)
    exec_ctx = PipelineExecutionContext(
        state=new_pipeline_state(),
        company=company,
        vision_path=vision,
        vision_content=vision.read_text(encoding="utf-8"),
        llm=mock_llm,
        options=PipelineRunOptions(no_commit=no_commit),
    )
    return exec_ctx, architecture_json, execution_plan_json, roster_json, paths


def _make_two_turn_exec_ctx(
    tmp_path: Path,
    mock_llm: MagicMock,
    *,
    no_commit: bool,
) -> tuple[PipelineExecutionContext, str, str, str, PhaseArtifactPaths]:
    """Return a two-turn execution context for invalidation and resume tests."""
    _setup_git_repo(tmp_path)
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n\nBuild the implementation slice.\n", encoding="utf-8")

    company = init_company(tmp_path)
    ensure_validation_contract(company)
    (company / "roles" / "python_backend_developer.md").write_text(_CANNED_ROLE, encoding="utf-8")

    architecture_json = _extract_json_block(_CANNED_ARCH)
    execution_plan_json = _single_turn_execution_plan_json()
    roster_json = _single_turn_roster_json()
    (company / "artifacts" / "architecture.json").write_text(architecture_json, encoding="utf-8")
    (company / "artifacts" / "execution_plan.json").write_text(execution_plan_json, encoding="utf-8")
    (company / "artifacts" / "roster.json").write_text(roster_json, encoding="utf-8")

    paths = _write_two_turn_phase_artifacts(company)
    exec_ctx = PipelineExecutionContext(
        state=new_pipeline_state(),
        company=company,
        vision_path=vision,
        vision_content=vision.read_text(encoding="utf-8"),
        llm=mock_llm,
        options=PipelineRunOptions(no_commit=no_commit),
    )
    return exec_ctx, architecture_json, execution_plan_json, roster_json, paths


def _make_single_turn_non_git_exec_ctx(
    tmp_path: Path,
    mock_llm: MagicMock,
    *,
    no_commit: bool,
) -> tuple[PipelineExecutionContext, str, str, str, PhaseArtifactPaths]:
    """Return a single-turn execution context without a git repository."""
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n\nBuild the implementation slice.\n", encoding="utf-8")

    company = init_company(tmp_path)
    ensure_validation_contract(company)
    (company / "roles" / "python_backend_developer.md").write_text(_CANNED_ROLE, encoding="utf-8")

    architecture_json = _extract_json_block(_CANNED_ARCH)
    execution_plan_json = _single_turn_execution_plan_json()
    roster_json = _single_turn_roster_json()
    (company / "artifacts" / "architecture.json").write_text(architecture_json, encoding="utf-8")
    (company / "artifacts" / "execution_plan.json").write_text(execution_plan_json, encoding="utf-8")
    (company / "artifacts" / "roster.json").write_text(roster_json, encoding="utf-8")

    paths = _write_single_turn_phase_artifacts(company)
    exec_ctx = PipelineExecutionContext(
        state=new_pipeline_state(),
        company=company,
        vision_path=vision,
        vision_content=vision.read_text(encoding="utf-8"),
        llm=mock_llm,
        options=PipelineRunOptions(no_commit=no_commit),
    )
    return exec_ctx, architecture_json, execution_plan_json, roster_json, paths


def _review_payload(  # pylint: disable=too-many-arguments
    decision: str,
    *,
    summary: str,
    scope_findings: list[str] | None = None,
    standards_findings: list[str] | None = None,
    validation_findings: list[str] | None = None,
    required_follow_up: list[str] | None = None,
) -> str:
    """Return a fenced JSON Development Lead review payload."""
    return (
        "```json\n"
        + json.dumps(
            {
                "decision": decision,
                "summary": summary,
                "scope_findings": scope_findings or [],
                "standards_findings": standards_findings or [],
                "validation_findings": validation_findings or [],
                "required_follow_up": required_follow_up or [],
            },
            indent=2,
        )
        + "\n```"
    )


def test_run_phase_implementation_loop_retries_same_turn_with_review_feedback(tmp_path: Path) -> None:
    """A revise decision should rerun the same turn and pass review follow-up into the next plan."""
    mock_llm = _configure_mock_llm(
        MagicMock(),
        invoke_responses=[
            _review_payload(
                "revise",
                summary="The turn needs a narrower scope.",
                scope_findings=["Touched files outside the approved task boundary."],
                required_follow_up=["Reduce the change to the approved task boundary."],
            ),
            _review_payload(
                "approve",
                summary="The revised turn stayed in scope and the validations remain adequate.",
            ),
        ],
    )
    execute_attempts = {"count": 0}

    def execute_side_effect(*_args: object, **_kwargs: object) -> str:
        execute_attempts["count"] += 1
        (tmp_path / "feature.py").write_text(f"print({execute_attempts['count']})\n", encoding="utf-8")
        return (
            "# Implementation Execution: phase_1 - Implementation Slice Turn 1\n\n"
            "## Completed Work\n- Updated the feature slice.\n\n"
            "## Files Changed\n- feature.py\n\n"
            "## Validation Notes\n- Validation contract reruns in orchestrator.\n\n"
            "## Follow-Up\n- None.\n"
        )

    mock_llm.invoke_plan.side_effect = [
        _implementation_plan_responses()[0],
        _implementation_plan_responses()[0].replace("approved environment updates", "narrowed approved updates"),
    ]
    mock_llm.invoke_execute.side_effect = execute_side_effect

    exec_ctx, architecture_json, execution_plan_json, roster_json, paths = _make_single_turn_exec_ctx(
        tmp_path,
        mock_llm,
        no_commit=True,
    )

    result = _run_phase_implementation_loop(
        exec_ctx,
        architecture_json=architecture_json,
        execution_plan_json=execution_plan_json,
        roster_json=roster_json,
    )

    assert result is None
    assert mock_llm.invoke_plan.call_count == 2
    assert mock_llm.invoke_execute.call_count == 2
    second_plan_prompt = mock_llm.invoke_plan.call_args_list[1].args[1]
    assert "Reduce the change to the approved task boundary." in second_plan_prompt
    assert paths.implementation_plan_path(1, "Python Backend Developer", 1).is_file()
    assert paths.implementation_plan_path(1, "Python Backend Developer", 2).is_file()
    assert paths.implementation_review_path(1, "Python Backend Developer", 1).is_file()
    assert paths.implementation_review_path(1, "Python Backend Developer", 2).is_file()


def test_run_phase_implementation_loop_retries_when_validation_fails(tmp_path: Path) -> None:
    """Failing command validations should block commit and force another turn attempt."""
    mock_llm = _configure_mock_llm(
        MagicMock(),
        invoke_responses=[
            _review_payload("approve", summary="Scope and standards look good."),
            _review_payload("approve", summary="Scope and standards look good."),
        ],
    )
    execute_attempts = {"count": 0}

    def execute_side_effect(*_args: object, **_kwargs: object) -> str:
        execute_attempts["count"] += 1
        (tmp_path / "feature.py").write_text(f"print({execute_attempts['count']})\n", encoding="utf-8")
        return _implementation_execute_responses()[0]

    mock_llm.invoke_plan.side_effect = [_implementation_plan_responses()[0], _implementation_plan_responses()[0]]
    mock_llm.invoke_execute.side_effect = execute_side_effect
    exec_ctx, architecture_json, execution_plan_json, roster_json, _paths = _make_single_turn_exec_ctx(
        tmp_path,
        mock_llm,
        no_commit=False,
    )
    failed_report = ValidationRunReport(
        results=[
            ValidationCheckResult(
                validation_id="unit",
                title="Unit tests",
                kind="command",
                status="failed",
                success_criteria=["Tests pass"],
                protects=["Implementation slice"],
                always_run=False,
                command="pytest",
                working_directory=".",
                exit_code=1,
                stdout="",
                stderr="failure",
            )
        ]
    )
    passed_report = ValidationRunReport(results=[])

    with (
        patch("asw.orchestrator.run_validation_contract", side_effect=[failed_report, passed_report]),
        patch("asw.orchestrator.commit_state", return_value="abc123") as mock_commit,
    ):
        result = _run_phase_implementation_loop(
            exec_ctx,
            architecture_json=architecture_json,
            execution_plan_json=execution_plan_json,
            roster_json=roster_json,
        )

    assert result is None
    assert mock_llm.invoke_plan.call_count == 2
    assert mock_llm.invoke_execute.call_count == 2
    second_plan_prompt = mock_llm.invoke_plan.call_args_list[1].args[1]
    assert "Fix the failing command validations before rerunning this same turn." in second_plan_prompt
    mock_commit.assert_called_once()


def test_run_phase_implementation_loop_persists_turn_step_metadata(  # pylint: disable=too-many-locals
    tmp_path: Path,
) -> None:
    """A successful turn should persist the expected metadata for each saved step."""
    mock_llm = _configure_mock_llm(
        MagicMock(),
        invoke_responses=[_review_payload("approve", summary="Scope and standards look good.")],
    )

    def execute_side_effect(*_args: object, **_kwargs: object) -> str:
        (tmp_path / "feature.py").write_text("print('done')\n", encoding="utf-8")
        return _implementation_execute_responses()[0]

    mock_llm.invoke_plan.side_effect = [_implementation_plan_responses()[0]]
    mock_llm.invoke_execute.side_effect = execute_side_effect
    exec_ctx, architecture_json, execution_plan_json, roster_json, _paths = _make_single_turn_exec_ctx(
        tmp_path,
        mock_llm,
        no_commit=False,
    )

    with patch("asw.orchestrator.commit_state", return_value="abc123"):
        result = _run_phase_implementation_loop(
            exec_ctx,
            architecture_json=architecture_json,
            execution_plan_json=execution_plan_json,
            roster_json=roster_json,
        )

    assert result is None

    state = read_pipeline_state(tmp_path)
    assert state == exec_ctx.state
    assert state is not None

    plan_record = state["phases"]["phase-loop:phase_1:turn:1:plan"]
    execute_record = state["phases"]["phase-loop:phase_1:turn:1:execute"]
    validate_record = state["phases"]["phase-loop:phase_1:turn:1:validate"]
    review_record = state["phases"]["phase-loop:phase_1:turn:1:review"]
    commit_record = state["phases"]["phase-loop:phase_1:turn:1:commit"]

    expected_common_metadata = {
        "owner_title": "Python Backend Developer",
        "task_ids": ["implement_slice"],
        "attempt": 1,
        "baseline_changed_paths": ["vision.md"],
    }

    assert plan_record["metadata"] == expected_common_metadata
    assert execute_record["metadata"] == expected_common_metadata
    assert validate_record["metadata"] == {
        **expected_common_metadata,
        "passed": True,
    }
    assert review_record["metadata"] == {
        **expected_common_metadata,
        "decision": "approve",
        "summary": "Scope and standards look good.",
        "scope_findings": [],
        "standards_findings": [],
        "validation_findings": [],
        "required_follow_up": [],
        "approved_paths": ["feature.py"],
        "changed_paths": ["feature.py"],
    }
    assert commit_record["metadata"] == {
        **expected_common_metadata,
        "approved_paths": ["feature.py"],
        "commit_hash": "abc123",
    }


def test_run_phase_implementation_loop_skips_current_committed_turns_on_resume(tmp_path: Path) -> None:
    """A turn with a current commit record should be skipped on rerun."""
    mock_llm = _configure_mock_llm(
        MagicMock(),
        invoke_responses=[_review_payload("approve", summary="Scope and standards look good.")],
    )

    def execute_side_effect(*_args: object, **_kwargs: object) -> str:
        (tmp_path / "feature.py").write_text("print('done')\n", encoding="utf-8")
        return _implementation_execute_responses()[0]

    mock_llm.invoke_plan.side_effect = [_implementation_plan_responses()[0]]
    mock_llm.invoke_execute.side_effect = execute_side_effect
    exec_ctx, architecture_json, execution_plan_json, roster_json, paths = _make_single_turn_exec_ctx(
        tmp_path,
        mock_llm,
        no_commit=False,
    )

    with patch("asw.orchestrator.commit_state", return_value="abc123"):
        first_result = _run_phase_implementation_loop(
            exec_ctx,
            architecture_json=architecture_json,
            execution_plan_json=execution_plan_json,
            roster_json=roster_json,
        )

    assert first_result is None
    assert paths.implementation_commit_path(1, "Python Backend Developer", 1).is_file()

    resumed_llm = MagicMock()
    resumed_llm.invoke_plan.side_effect = AssertionError("invoke_plan should not run for a committed current turn")
    resumed_llm.invoke_execute.side_effect = AssertionError(
        "invoke_execute should not run for a committed current turn"
    )
    resumed_llm.invoke.side_effect = AssertionError("review should not run for a committed current turn")
    exec_ctx.llm = resumed_llm

    with patch("asw.orchestrator.commit_state", side_effect=AssertionError("commit should not rerun")):
        second_result = _run_phase_implementation_loop(
            exec_ctx,
            architecture_json=architecture_json,
            execution_plan_json=execution_plan_json,
            roster_json=roster_json,
        )

    assert second_result is None


def test_run_phase_implementation_loop_retries_commit_without_rerunning_turn(tmp_path: Path) -> None:
    """A failed commit should resume from the saved approved review state."""
    mock_llm = _configure_mock_llm(
        MagicMock(),
        invoke_responses=[_review_payload("approve", summary="Scope and standards look good.")],
    )

    def execute_side_effect(*_args: object, **_kwargs: object) -> str:
        (tmp_path / "feature.py").write_text("print('done')\n", encoding="utf-8")
        return _implementation_execute_responses()[0]

    mock_llm.invoke_plan.side_effect = [_implementation_plan_responses()[0]]
    mock_llm.invoke_execute.side_effect = execute_side_effect
    exec_ctx, architecture_json, execution_plan_json, roster_json, paths = _make_single_turn_exec_ctx(
        tmp_path,
        mock_llm,
        no_commit=False,
    )

    with patch("asw.orchestrator.commit_state", side_effect=GitError("commit failed")):
        first_result = _run_phase_implementation_loop(
            exec_ctx,
            architecture_json=architecture_json,
            execution_plan_json=execution_plan_json,
            roster_json=roster_json,
        )

    assert first_result == 1
    assert paths.implementation_review_path(1, "Python Backend Developer", 1).is_file()

    resumed_llm = MagicMock()
    resumed_llm.invoke_plan.side_effect = AssertionError("plan should not rerun on commit retry")
    resumed_llm.invoke_execute.side_effect = AssertionError("execute should not rerun on commit retry")
    resumed_llm.invoke.side_effect = AssertionError("review should not rerun on commit retry")
    exec_ctx.llm = resumed_llm

    with patch("asw.orchestrator.commit_state", return_value="def456") as mock_commit:
        second_result = _run_phase_implementation_loop(
            exec_ctx,
            architecture_json=architecture_json,
            execution_plan_json=execution_plan_json,
            roster_json=roster_json,
        )

    assert second_result is None
    assert paths.implementation_commit_path(1, "Python Backend Developer", 1).is_file()
    mock_commit.assert_called_once()


def test_run_phase_implementation_loop_resumes_review_when_review_artifact_missing(tmp_path: Path) -> None:
    """A missing review artifact should resume from review without rerunning plan or execute."""
    mock_llm = _configure_mock_llm(
        MagicMock(),
        invoke_responses=[_review_payload("approve", summary="Scope and standards look good.")],
    )

    def execute_side_effect(*_args: object, **_kwargs: object) -> str:
        (tmp_path / "feature.py").write_text("print('done')\n", encoding="utf-8")
        return _implementation_execute_responses()[0]

    mock_llm.invoke_plan.side_effect = [_implementation_plan_responses()[0]]
    mock_llm.invoke_execute.side_effect = execute_side_effect
    exec_ctx, architecture_json, execution_plan_json, roster_json, paths = _make_single_turn_exec_ctx(
        tmp_path,
        mock_llm,
        no_commit=True,
    )

    first_result = _run_phase_implementation_loop(
        exec_ctx,
        architecture_json=architecture_json,
        execution_plan_json=execution_plan_json,
        roster_json=roster_json,
    )

    assert first_result is None
    paths.implementation_review_path(1, "Python Backend Developer", 1).unlink()

    resumed_llm = _configure_mock_llm(
        MagicMock(),
        invoke_responses=[_review_payload("approve", summary="Scope and standards look good.")],
    )
    resumed_llm.invoke_plan.side_effect = AssertionError("plan should not rerun when only review is missing")
    resumed_llm.invoke_execute.side_effect = AssertionError("execute should not rerun when only review is missing")
    exec_ctx.llm = resumed_llm

    second_result = _run_phase_implementation_loop(
        exec_ctx,
        architecture_json=architecture_json,
        execution_plan_json=execution_plan_json,
        roster_json=roster_json,
    )

    assert second_result is None
    assert resumed_llm.invoke.call_count == 1


def test_run_phase_implementation_loop_refuses_commit_when_unapproved_paths_appear(tmp_path: Path) -> None:
    """An extra turn-scoped path after review should block commit."""
    mock_llm = _configure_mock_llm(
        MagicMock(),
        invoke_responses=[_review_payload("approve", summary="Scope and standards look good.")],
    )

    def execute_side_effect(*_args: object, **_kwargs: object) -> str:
        (tmp_path / "feature.py").write_text("print('done')\n", encoding="utf-8")
        return _implementation_execute_responses()[0]

    mock_llm.invoke_plan.side_effect = [_implementation_plan_responses()[0]]
    mock_llm.invoke_execute.side_effect = execute_side_effect
    exec_ctx, architecture_json, execution_plan_json, roster_json, _paths = _make_single_turn_exec_ctx(
        tmp_path,
        mock_llm,
        no_commit=False,
    )
    original_review = getattr(orchestrator_module, "_run_development_lead_review")

    def review_with_extra_path(*args: object, **kwargs: object) -> tuple[dict[str, object] | None, int | None]:
        review, err = original_review(*args, **kwargs)
        (tmp_path / "unexpected.py").write_text("print('unexpected')\n", encoding="utf-8")
        return review, err

    with (
        patch("asw.orchestrator._run_development_lead_review", side_effect=review_with_extra_path),
        patch("asw.orchestrator.commit_state", side_effect=AssertionError("commit must not run")),
    ):
        result = _run_phase_implementation_loop(
            exec_ctx,
            architecture_json=architecture_json,
            execution_plan_json=execution_plan_json,
            roster_json=roster_json,
        )

    assert result == 1


def test_run_phase_implementation_loop_allows_stage_all_override_for_extra_paths(tmp_path: Path) -> None:
    """Stage-all commits should bypass the approved-path scope guard intentionally."""
    mock_llm = _configure_mock_llm(
        MagicMock(),
        invoke_responses=[_review_payload("approve", summary="Scope and standards look good.")],
    )

    def execute_side_effect(*_args: object, **_kwargs: object) -> str:
        (tmp_path / "feature.py").write_text("print('done')\n", encoding="utf-8")
        return _implementation_execute_responses()[0]

    mock_llm.invoke_plan.side_effect = [_implementation_plan_responses()[0]]
    mock_llm.invoke_execute.side_effect = execute_side_effect
    exec_ctx, architecture_json, execution_plan_json, roster_json, _paths = _make_single_turn_exec_ctx(
        tmp_path,
        mock_llm,
        no_commit=False,
    )
    exec_ctx.options = PipelineRunOptions(no_commit=False, stage_all=True)
    original_review = getattr(orchestrator_module, "_run_development_lead_review")

    def review_with_extra_path(*args: object, **kwargs: object) -> tuple[dict[str, object] | None, int | None]:
        review, err = original_review(*args, **kwargs)
        (tmp_path / "unexpected.py").write_text("print('unexpected')\n", encoding="utf-8")
        return review, err

    with (
        patch("asw.orchestrator._run_development_lead_review", side_effect=review_with_extra_path),
        patch("asw.orchestrator.commit_state", return_value="abc123") as mock_commit,
    ):
        result = _run_phase_implementation_loop(
            exec_ctx,
            architecture_json=architecture_json,
            execution_plan_json=execution_plan_json,
            roster_json=roster_json,
        )

    assert result is None
    assert mock_commit.call_count == 1
    assert mock_commit.call_args.kwargs["stage_all"] is True
    assert mock_commit.call_args.kwargs["approved_paths"] is None


def test_run_phase_implementation_loop_resumes_safely_in_non_git_workdir(tmp_path: Path) -> None:
    """A non-git workdir should persist empty changed-path evidence and still resume safely."""
    mock_llm = _configure_mock_llm(
        MagicMock(),
        invoke_responses=[_review_payload("approve", summary="Scope and standards look good.")],
    )
    mock_llm.invoke_plan.side_effect = [_implementation_plan_responses()[0]]
    mock_llm.invoke_execute.side_effect = [_implementation_execute_responses()[0]]
    exec_ctx, architecture_json, execution_plan_json, roster_json, paths = _make_single_turn_non_git_exec_ctx(
        tmp_path,
        mock_llm,
        no_commit=False,
    )

    with patch("asw.orchestrator.commit_state", return_value="abc123"):
        first_result = _run_phase_implementation_loop(
            exec_ctx,
            architecture_json=architecture_json,
            execution_plan_json=execution_plan_json,
            roster_json=roster_json,
        )

    assert first_result is None
    state = read_pipeline_state(tmp_path)
    assert state is not None
    review_metadata = state["phases"]["phase-loop:phase_1:turn:1:review"]["metadata"]
    commit_metadata = state["phases"]["phase-loop:phase_1:turn:1:commit"]["metadata"]
    assert review_metadata["changed_paths"] == []
    assert review_metadata["approved_paths"] == []
    assert commit_metadata["approved_paths"] == []
    assert paths.implementation_commit_path(1, "Python Backend Developer", 1).is_file()

    resumed_llm = MagicMock()
    resumed_llm.invoke_plan.side_effect = AssertionError("plan should not rerun for a current committed turn")
    resumed_llm.invoke_execute.side_effect = AssertionError("execute should not rerun for a current committed turn")
    resumed_llm.invoke.side_effect = AssertionError("review should not rerun for a current committed turn")
    exec_ctx.llm = resumed_llm

    with patch("asw.orchestrator.commit_state", side_effect=AssertionError("commit should not rerun")):
        second_result = _run_phase_implementation_loop(
            exec_ctx,
            architecture_json=architecture_json,
            execution_plan_json=execution_plan_json,
            roster_json=roster_json,
        )

    assert second_result is None


def test_run_phase_implementation_loop_persists_no_op_commit_and_skips_it_on_rerun(tmp_path: Path) -> None:
    """An empty commit hash should still persist durable no-op commit evidence."""
    mock_llm = _configure_mock_llm(
        MagicMock(),
        invoke_responses=[_review_payload("approve", summary="Scope and standards look good.")],
    )
    mock_llm.invoke_plan.side_effect = [_implementation_plan_responses()[0]]
    mock_llm.invoke_execute.side_effect = [_implementation_execute_responses()[0]]
    exec_ctx, architecture_json, execution_plan_json, roster_json, paths = _make_single_turn_exec_ctx(
        tmp_path,
        mock_llm,
        no_commit=False,
    )

    with patch("asw.orchestrator.commit_state", return_value=""):
        first_result = _run_phase_implementation_loop(
            exec_ctx,
            architecture_json=architecture_json,
            execution_plan_json=execution_plan_json,
            roster_json=roster_json,
        )

    assert first_result is None
    state = read_pipeline_state(tmp_path)
    assert state is not None
    commit_metadata = state["phases"]["phase-loop:phase_1:turn:1:commit"]["metadata"]
    commit_summary = paths.implementation_commit_path(1, "Python Backend Developer", 1).read_text(encoding="utf-8")
    assert commit_metadata["approved_paths"] == []
    assert commit_metadata["commit_hash"] == ""
    assert "(no commit created)" in commit_summary

    resumed_llm = MagicMock()
    resumed_llm.invoke_plan.side_effect = AssertionError("plan should not rerun for a current no-op commit")
    resumed_llm.invoke_execute.side_effect = AssertionError("execute should not rerun for a current no-op commit")
    resumed_llm.invoke.side_effect = AssertionError("review should not rerun for a current no-op commit")
    exec_ctx.llm = resumed_llm

    with patch("asw.orchestrator.commit_state", side_effect=AssertionError("commit should not rerun")):
        second_result = _run_phase_implementation_loop(
            exec_ctx,
            architecture_json=architecture_json,
            execution_plan_json=execution_plan_json,
            roster_json=roster_json,
        )

    assert second_result is None


def test_run_phase_implementation_loop_persists_skipped_no_commit_and_skips_it_on_rerun(tmp_path: Path) -> None:
    """The --no-commit path should still persist commit-step evidence for resume logic."""
    mock_llm = _configure_mock_llm(
        MagicMock(),
        invoke_responses=[_review_payload("approve", summary="Scope and standards look good.")],
    )

    def execute_side_effect(*_args: object, **_kwargs: object) -> str:
        (tmp_path / "feature.py").write_text("print('done')\n", encoding="utf-8")
        return _implementation_execute_responses()[0]

    mock_llm.invoke_plan.side_effect = [_implementation_plan_responses()[0]]
    mock_llm.invoke_execute.side_effect = execute_side_effect
    exec_ctx, architecture_json, execution_plan_json, roster_json, paths = _make_single_turn_exec_ctx(
        tmp_path,
        mock_llm,
        no_commit=True,
    )

    first_result = _run_phase_implementation_loop(
        exec_ctx,
        architecture_json=architecture_json,
        execution_plan_json=execution_plan_json,
        roster_json=roster_json,
    )

    assert first_result is None
    state = read_pipeline_state(tmp_path)
    assert state is not None
    commit_metadata = state["phases"]["phase-loop:phase_1:turn:1:commit"]["metadata"]
    commit_summary = paths.implementation_commit_path(1, "Python Backend Developer", 1).read_text(encoding="utf-8")
    assert commit_metadata["approved_paths"] == ["feature.py"]
    assert commit_metadata["commit_hash"] == ""
    assert "(no commit created)" in commit_summary

    resumed_llm = MagicMock()
    resumed_llm.invoke_plan.side_effect = AssertionError("plan should not rerun for a current --no-commit turn")
    resumed_llm.invoke_execute.side_effect = AssertionError("execute should not rerun for a current --no-commit turn")
    resumed_llm.invoke.side_effect = AssertionError("review should not rerun for a current --no-commit turn")
    exec_ctx.llm = resumed_llm

    second_result = _run_phase_implementation_loop(
        exec_ctx,
        architecture_json=architecture_json,
        execution_plan_json=execution_plan_json,
        roster_json=roster_json,
    )

    assert second_result is None


def test_run_phase_implementation_loop_invalidates_downstream_turns_when_validation_contract_changes(  # pylint: disable=too-many-locals
    tmp_path: Path,
) -> None:
    """A changed validation contract should rerun the affected turn and later turns."""
    _setup_git_repo(tmp_path)
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n\nBuild the implementation slice.\n", encoding="utf-8")
    company = init_company(tmp_path)
    ensure_validation_contract(company)
    (company / "roles" / "python_backend_developer.md").write_text(_CANNED_ROLE, encoding="utf-8")
    architecture_json = _extract_json_block(_CANNED_ARCH)
    execution_plan_json = _single_turn_execution_plan_json()
    roster_json = _single_turn_roster_json()
    (company / "artifacts" / "architecture.json").write_text(architecture_json, encoding="utf-8")
    (company / "artifacts" / "execution_plan.json").write_text(execution_plan_json, encoding="utf-8")
    (company / "artifacts" / "roster.json").write_text(roster_json, encoding="utf-8")
    paths = build_phase_artifact_paths(company, 0)
    paths.final_path.parent.mkdir(parents=True, exist_ok=True)
    paths.final_path.write_text(
        "# Phase Design: Implementation Slice\n\n## Phase Summary\n- Deliver the current slice.\n",
        encoding="utf-8",
    )
    write_phase_task_mapping(
        {
            "tasks": [
                {
                    "id": "implement_first",
                    "title": "Implement the first slice",
                    "owner": "Python Backend Developer",
                    "objective": "Apply the first implementation change.",
                    "depends_on": [],
                    "deliverables": ["Code change"],
                    "acceptance_criteria": ["Validation remains adequate"],
                },
                {
                    "id": "implement_second",
                    "title": "Implement the second slice",
                    "owner": "Python Backend Developer",
                    "objective": "Apply the second implementation change.",
                    "depends_on": ["implement_first"],
                    "deliverables": ["Code change"],
                    "acceptance_criteria": ["Validation remains adequate"],
                },
            ]
        },
        paths,
        phase_label="phase_1 - Implementation Slice",
    )

    first_llm = _configure_mock_llm(
        MagicMock(),
        invoke_responses=[
            _review_payload("approve", summary="First turn approved."),
            _review_payload("approve", summary="Second turn approved."),
        ],
    )
    execute_attempts = {"count": 0}

    def execute_side_effect(*_args: object, **_kwargs: object) -> str:
        execute_attempts["count"] += 1
        (tmp_path / f"feature_{execute_attempts['count']}.py").write_text("print('done')\n", encoding="utf-8")
        return _implementation_execute_responses()[0]

    first_llm.invoke_plan.side_effect = [_implementation_plan_responses()[0], _implementation_plan_responses()[0]]
    first_llm.invoke_execute.side_effect = execute_side_effect
    exec_ctx = PipelineExecutionContext(
        state=new_pipeline_state(),
        company=company,
        vision_path=vision,
        vision_content=vision.read_text(encoding="utf-8"),
        llm=first_llm,
        options=PipelineRunOptions(no_commit=False),
    )

    with patch("asw.orchestrator.commit_state", side_effect=["abc123", "def456"]):
        first_result = _run_phase_implementation_loop(
            exec_ctx,
            architecture_json=architecture_json,
            execution_plan_json=execution_plan_json,
            roster_json=roster_json,
        )

    assert first_result is None

    contract_json_path, _ = validation_contract_paths(company)
    contract = json.loads(contract_json_path.read_text(encoding="utf-8"))
    contract["summary"] = "Validation coverage changed after the saved implementation turns."
    contract_json_path.write_text(json.dumps(contract, indent=2) + "\n", encoding="utf-8")

    resumed_llm = _configure_mock_llm(
        MagicMock(),
        invoke_responses=[
            _review_payload("approve", summary="First rerun approved."),
            _review_payload("approve", summary="Second rerun approved."),
        ],
    )
    resumed_llm.invoke_plan.side_effect = [_implementation_plan_responses()[0], _implementation_plan_responses()[0]]
    resumed_llm.invoke_execute.side_effect = execute_side_effect
    exec_ctx.llm = resumed_llm

    with patch("asw.orchestrator.commit_state", side_effect=["ghi789", "jkl012"]):
        second_result = _run_phase_implementation_loop(
            exec_ctx,
            architecture_json=architecture_json,
            execution_plan_json=execution_plan_json,
            roster_json=roster_json,
        )

    assert second_result is None
    assert resumed_llm.invoke_plan.call_count == 2


def test_run_phase_implementation_loop_invalidates_downstream_turns_when_phase_design_changes(
    tmp_path: Path,
) -> None:
    """A changed final phase design should rerun the affected turn and later turns."""
    first_llm = _configure_mock_llm(
        MagicMock(),
        invoke_responses=[
            _review_payload("approve", summary="First turn approved."),
            _review_payload("approve", summary="Second turn approved."),
        ],
    )
    execute_attempts = {"count": 0}

    def execute_side_effect(*_args: object, **_kwargs: object) -> str:
        execute_attempts["count"] += 1
        (tmp_path / f"feature_{execute_attempts['count']}.py").write_text("print('done')\n", encoding="utf-8")
        return _implementation_execute_responses()[0]

    first_llm.invoke_plan.side_effect = [_implementation_plan_responses()[0], _implementation_plan_responses()[0]]
    first_llm.invoke_execute.side_effect = execute_side_effect
    exec_ctx, architecture_json, execution_plan_json, roster_json, paths = _make_two_turn_exec_ctx(
        tmp_path,
        first_llm,
        no_commit=False,
    )

    with patch("asw.orchestrator.commit_state", side_effect=["abc123", "def456"]):
        first_result = _run_phase_implementation_loop(
            exec_ctx,
            architecture_json=architecture_json,
            execution_plan_json=execution_plan_json,
            roster_json=roster_json,
        )

    assert first_result is None

    paths.final_path.write_text(
        "# Phase Design: Implementation Slice\n\n## Phase Summary\n"
        "- Deliver the current slice with updated sequencing guidance.\n",
        encoding="utf-8",
    )

    resumed_llm = _configure_mock_llm(
        MagicMock(),
        invoke_responses=[
            _review_payload("approve", summary="First rerun approved."),
            _review_payload("approve", summary="Second rerun approved."),
        ],
    )
    resumed_llm.invoke_plan.side_effect = [_implementation_plan_responses()[0], _implementation_plan_responses()[0]]
    resumed_llm.invoke_execute.side_effect = execute_side_effect
    exec_ctx.llm = resumed_llm

    with patch("asw.orchestrator.commit_state", side_effect=["ghi789", "jkl012"]):
        second_result = _run_phase_implementation_loop(
            exec_ctx,
            architecture_json=architecture_json,
            execution_plan_json=execution_plan_json,
            roster_json=roster_json,
        )

    assert second_result is None
    assert resumed_llm.invoke_plan.call_count == 2
    state = read_pipeline_state(tmp_path)
    assert state is not None
    assert state["phases"]["phase-loop:phase_1:turn:1:commit"]["metadata"]["attempt"] == 2
    assert state["phases"]["phase-loop:phase_1:turn:2:commit"]["metadata"]["attempt"] == 2


def test_run_phase_implementation_loop_invalidates_downstream_turns_when_role_prompt_changes(
    tmp_path: Path,
) -> None:
    """A changed owner role file should rerun the affected turn and later turns."""
    first_llm = _configure_mock_llm(
        MagicMock(),
        invoke_responses=[
            _review_payload("approve", summary="First turn approved."),
            _review_payload("approve", summary="Second turn approved."),
        ],
    )
    execute_attempts = {"count": 0}

    def execute_side_effect(*_args: object, **_kwargs: object) -> str:
        execute_attempts["count"] += 1
        (tmp_path / f"feature_{execute_attempts['count']}.py").write_text("print('done')\n", encoding="utf-8")
        return _implementation_execute_responses()[0]

    first_llm.invoke_plan.side_effect = [_implementation_plan_responses()[0], _implementation_plan_responses()[0]]
    first_llm.invoke_execute.side_effect = execute_side_effect
    exec_ctx, architecture_json, execution_plan_json, roster_json, _paths = _make_two_turn_exec_ctx(
        tmp_path,
        first_llm,
        no_commit=False,
    )

    with patch("asw.orchestrator.commit_state", side_effect=["abc123", "def456"]):
        first_result = _run_phase_implementation_loop(
            exec_ctx,
            architecture_json=architecture_json,
            execution_plan_json=execution_plan_json,
            roster_json=roster_json,
        )

    assert first_result is None

    role_path = exec_ctx.company / "roles" / "python_backend_developer.md"
    role_path.write_text(role_path.read_text(encoding="utf-8") + "\nUpdated role guidance.\n", encoding="utf-8")

    resumed_llm = _configure_mock_llm(
        MagicMock(),
        invoke_responses=[
            _review_payload("approve", summary="First rerun approved."),
            _review_payload("approve", summary="Second rerun approved."),
        ],
    )
    resumed_llm.invoke_plan.side_effect = [_implementation_plan_responses()[0], _implementation_plan_responses()[0]]
    resumed_llm.invoke_execute.side_effect = execute_side_effect
    exec_ctx.llm = resumed_llm

    with patch("asw.orchestrator.commit_state", side_effect=["ghi789", "jkl012"]):
        second_result = _run_phase_implementation_loop(
            exec_ctx,
            architecture_json=architecture_json,
            execution_plan_json=execution_plan_json,
            roster_json=roster_json,
        )

    assert second_result is None
    assert resumed_llm.invoke_plan.call_count == 2
    state = read_pipeline_state(tmp_path)
    assert state is not None
    assert state["phases"]["phase-loop:phase_1:turn:1:commit"]["metadata"]["attempt"] == 2
    assert state["phases"]["phase-loop:phase_1:turn:2:commit"]["metadata"]["attempt"] == 2


def test_run_phase_implementation_loop_invalidates_downstream_turns_when_assigned_standards_change(
    tmp_path: Path,
) -> None:
    """A changed assigned standards file should rerun the affected turn and later turns."""
    first_llm = _configure_mock_llm(
        MagicMock(),
        invoke_responses=[
            _review_payload("approve", summary="First turn approved."),
            _review_payload("approve", summary="Second turn approved."),
        ],
    )
    execute_attempts = {"count": 0}

    def execute_side_effect(*_args: object, **_kwargs: object) -> str:
        execute_attempts["count"] += 1
        (tmp_path / f"feature_{execute_attempts['count']}.py").write_text("print('done')\n", encoding="utf-8")
        return _implementation_execute_responses()[0]

    first_llm.invoke_plan.side_effect = [_implementation_plan_responses()[0], _implementation_plan_responses()[0]]
    first_llm.invoke_execute.side_effect = execute_side_effect
    exec_ctx, architecture_json, execution_plan_json, roster_json, _paths = _make_two_turn_exec_ctx(
        tmp_path,
        first_llm,
        no_commit=False,
    )

    with patch("asw.orchestrator.commit_state", side_effect=["abc123", "def456"]):
        first_result = _run_phase_implementation_loop(
            exec_ctx,
            architecture_json=architecture_json,
            execution_plan_json=execution_plan_json,
            roster_json=roster_json,
        )

    assert first_result is None

    standards_path = exec_ctx.company / "standards" / "python_guidelines.md"
    standards_path.write_text(
        standards_path.read_text(encoding="utf-8") + "\nAdd one more repository-specific reminder.\n",
        encoding="utf-8",
    )

    resumed_llm = _configure_mock_llm(
        MagicMock(),
        invoke_responses=[
            _review_payload("approve", summary="First rerun approved."),
            _review_payload("approve", summary="Second rerun approved."),
        ],
    )
    resumed_llm.invoke_plan.side_effect = [_implementation_plan_responses()[0], _implementation_plan_responses()[0]]
    resumed_llm.invoke_execute.side_effect = execute_side_effect
    exec_ctx.llm = resumed_llm

    with patch("asw.orchestrator.commit_state", side_effect=["ghi789", "jkl012"]):
        second_result = _run_phase_implementation_loop(
            exec_ctx,
            architecture_json=architecture_json,
            execution_plan_json=execution_plan_json,
            roster_json=roster_json,
        )

    assert second_result is None
    assert resumed_llm.invoke_plan.call_count == 2
    state = read_pipeline_state(tmp_path)
    assert state is not None
    assert state["phases"]["phase-loop:phase_1:turn:1:commit"]["metadata"]["attempt"] == 2
    assert state["phases"]["phase-loop:phase_1:turn:2:commit"]["metadata"]["attempt"] == 2


def test_resume_skips_current_phase_design_and_reruns_only_downstream_turns_after_standards_change(  # pylint: disable=too-many-locals,too-many-statements
    tmp_path: Path,
) -> None:
    """A saved phase design should skip while stale later-turn standards rerun only affected turns."""
    _setup_git_repo(tmp_path)
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n\nBuild the implementation slice.\n", encoding="utf-8")
    company = init_company(tmp_path)
    ensure_validation_contract(company)
    (company / "artifacts" / "prd.md").write_text(_CANNED_PRD, encoding="utf-8")
    (company / "artifacts" / "architecture.json").write_text(_extract_json_block(_CANNED_ARCH), encoding="utf-8")
    execution_plan_json = _extract_json_block(_CANNED_EXECUTION_PLAN)
    roster_json = _extract_json_block(_CANNED_ROSTER)
    (company / "artifacts" / "execution_plan.json").write_text(execution_plan_json, encoding="utf-8")
    (company / "artifacts" / "roster.json").write_text(roster_json, encoding="utf-8")
    (company / "roles" / "python_backend_developer.md").write_text(_CANNED_ROLE, encoding="utf-8")

    phase_data = json.loads(execution_plan_json)["phases"][0]
    paths = build_phase_artifact_paths(company, 0)
    exec_ctx = PipelineExecutionContext(
        state=new_pipeline_state(),
        company=company,
        vision_path=vision,
        vision_content=vision.read_text(encoding="utf-8"),
        llm=MagicMock(),
        options=PipelineRunOptions(no_commit=False),
    )

    def fake_agent_loop(_agent: MagicMock, _context: dict, _lint_fn: MagicMock, label: str, **_kwargs: object) -> str:
        if label.endswith("Design Draft"):
            return _CANNED_PHASE_DESIGN_DRAFT
        if label.endswith("Feedback: Development Lead"):
            return _CANNED_PHASE_FEEDBACK_DEVELOPMENT_LEAD
        if label.endswith("Feedback: DevOps Engineer"):
            return _CANNED_PHASE_FEEDBACK_DEVOPS
        if label.endswith("Feedback: Python Backend Developer"):
            return _CANNED_PHASE_FEEDBACK_BACKEND
        if label.endswith("Design Final"):
            return _CANNED_PHASE_DESIGN_FINAL
        raise AssertionError(f"Unexpected label: {label}")

    with patch("asw.orchestrator._agent_loop", side_effect=fake_agent_loop):
        final = _run_or_skip_phase_design_step(
            exec_ctx,
            prd_content=_CANNED_PRD,
            architecture_json=_extract_json_block(_CANNED_ARCH),
            execution_plan_json=execution_plan_json,
            roster_json=roster_json,
            phase_data=phase_data,
            phase_index=0,
            paths=paths,
        )

    assert final == _CANNED_PHASE_DESIGN_FINAL

    first_impl_llm = _configure_mock_llm(
        MagicMock(),
        invoke_responses=[
            _review_payload("approve", summary="Turn 1 approved."),
            _review_payload("approve", summary="Turn 2 approved."),
            _review_payload("approve", summary="Turn 3 approved."),
        ],
    )
    execute_attempts = {"count": 0}

    def execute_side_effect(*_args: object, **_kwargs: object) -> str:
        execute_attempts["count"] += 1
        (tmp_path / f"feature_{execute_attempts['count']}.py").write_text("print('done')\n", encoding="utf-8")
        responses = _implementation_execute_responses()
        return responses[(execute_attempts["count"] - 1) % len(responses)]

    first_impl_llm.invoke_plan.side_effect = _implementation_plan_responses()
    first_impl_llm.invoke_execute.side_effect = execute_side_effect
    exec_ctx.llm = first_impl_llm

    first_result = _run_phase_implementation_loop(
        exec_ctx,
        architecture_json=_extract_json_block(_CANNED_ARCH),
        execution_plan_json=execution_plan_json,
        roster_json=roster_json,
    )

    assert first_result is None

    standards_path = company / "standards" / "python_guidelines.md"
    standards_path.write_text(
        standards_path.read_text(encoding="utf-8") + "\nTighten the backend implementation guidance.\n",
        encoding="utf-8",
    )

    with patch("asw.orchestrator._run_phase_design_step", side_effect=AssertionError("phase design should skip")):
        skipped_final = _run_or_skip_phase_design_step(
            exec_ctx,
            prd_content=_CANNED_PRD,
            architecture_json=_extract_json_block(_CANNED_ARCH),
            execution_plan_json=execution_plan_json,
            roster_json=roster_json,
            phase_data=phase_data,
            phase_index=0,
            paths=paths,
        )

    assert skipped_final == _CANNED_PHASE_DESIGN_FINAL

    resumed_llm = _configure_mock_llm(
        MagicMock(),
        invoke_responses=[
            _review_payload("approve", summary="Turn 2 rerun approved."),
            _review_payload("approve", summary="Turn 3 rerun approved."),
        ],
    )
    resumed_llm.invoke_plan.side_effect = _implementation_plan_responses()[1:]
    resumed_llm.invoke_execute.side_effect = execute_side_effect
    exec_ctx.llm = resumed_llm

    second_result = _run_phase_implementation_loop(
        exec_ctx,
        architecture_json=_extract_json_block(_CANNED_ARCH),
        execution_plan_json=execution_plan_json,
        roster_json=roster_json,
    )

    assert second_result is None
    assert resumed_llm.invoke_plan.call_count == 2

    state = read_pipeline_state(tmp_path)
    assert state is not None
    assert state["phases"]["phase-loop:phase_1:turn:1:commit"]["metadata"]["attempt"] == 1
    assert state["phases"]["phase-loop:phase_1:turn:2:commit"]["metadata"]["attempt"] == 2
    assert state["phases"]["phase-loop:phase_1:turn:3:commit"]["metadata"]["attempt"] == 2


def test_full_pipeline(tmp_path: Path) -> None:  # pylint: disable=too-many-locals
    """End-to-end: mock LLM + auto-approve → artifacts written."""
    # Write a minimal vision file.
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n\nBuild a CLI tool.\n")

    # Initialise a git repo so commit_state works.
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@test.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )

    mock_llm = _make_mock_llm()

    with (
        patch("asw.orchestrator.get_backend", return_value=mock_llm),
        patch("asw.orchestrator.founder_review", return_value=_APPROVE_REVIEW),
        patch("asw.orchestrator.founder_approve_devops_execution", return_value=_APPROVE_EXECUTION) as mock_approval,
        patch("asw.orchestrator.subprocess.run", return_value=_SUCCESSFUL_DEVOPS_EXECUTION) as mock_execute,
    ):
        result = run_pipeline(vision_path=vision, workdir=tmp_path)

    assert result == 0
    mock_approval.assert_not_called()
    bash_calls = [call for call in mock_execute.call_args_list if call.args and call.args[0][0] == "bash"]
    assert not bash_calls

    # Verify V0.1 artifacts were written.
    company = tmp_path / ".company"
    assert (company / "artifacts" / "prd.md").is_file()
    assert (company / "artifacts" / "architecture.json").is_file()
    assert (company / "artifacts" / "architecture.md").is_file()

    # Verify architecture JSON is valid.
    arch = json.loads((company / "artifacts" / "architecture.json").read_text())
    assert arch["project_name"] == "agenticorg"

    # Verify V0.2 hiring artifacts were written.
    assert (company / "artifacts" / "execution_plan.json").is_file()
    assert (company / "artifacts" / "execution_plan.md").is_file()
    assert (company / "artifacts" / "roster.json").is_file()
    assert (company / "artifacts" / "roster.md").is_file()

    execution_plan = json.loads((company / "artifacts" / "execution_plan.json").read_text())
    assert {entry["title"] for entry in execution_plan["selected_team"]} == {
        "Development Lead",
        "DevOps Engineer",
        "Python Backend Developer",
    }

    roster = json.loads((company / "artifacts" / "roster.json").read_text())
    assert len(roster["hired_agents"]) == 3
    backend_entry = next(entry for entry in roster["hired_agents"] if entry["title"] == "Python Backend Developer")
    assert backend_entry["mission"] == "Deliver the first validated backend workflow for Phase 1."

    # Verify generated role file was written.
    assert (company / "roles" / "development_lead.md").is_file()
    assert (company / "roles" / "devops_engineer.md").is_file()
    assert (company / "roles" / "python_backend_developer.md").is_file()
    role_content = (company / "roles" / "python_backend_developer.md").read_text()
    assert "# Role: Python Backend Developer" in role_content

    # Verify pipeline state was written.
    state = read_pipeline_state(tmp_path)
    assert state is not None
    for phase in (
        "prd",
        "architecture",
        "execution_plan",
        "roster",
        "roles",
        "phase-loop:phase_1:design",
        "phase-loop:phase_1:devops-proposal",
        "phase-loop:phase_1:devops-execution",
    ):
        assert phase in state["phases"]
    execution_phase = state["phases"]["phase-loop:phase_1:devops-execution"]
    assert execution_phase["metadata"]["status"] == "deferred"
    assert execution_phase["metadata"]["reason"] == (
        "Phase setup execution is deferred until the implementation loops are available."
    )
    phase_paths = build_phase_artifact_paths(company, 0)
    assert phase_paths.draft_path.is_file()
    assert phase_paths.final_path.is_file()
    assert phase_paths.task_mapping_json_path.is_file()
    assert phase_paths.task_mapping_md_path.is_file()
    assert phase_paths.proposal_path.is_file()
    assert phase_paths.summary_path.is_file()
    assert phase_paths.script_path.is_file()


def test_prd_founder_answers_are_applied_locally_without_extra_llm_call(tmp_path: Path) -> None:
    """Answering PRD founder questions should not trigger an extra Gemini call."""
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n\nBuild a CLI tool.\n")
    _setup_git_repo(tmp_path)

    mock_llm = _configure_mock_llm(
        MagicMock(),
        invoke_responses=[
            _CANNED_PRD_WITH_QUESTIONS,
            _CANNED_ARCH,
            _CANNED_EXECUTION_PLAN,
            _CANNED_ROSTER,
            _CANNED_ROLE,
            *_phase_preparation_responses(),
            *_implementation_review_responses(),
        ],
    )

    with (
        patch("asw.orchestrator.get_backend", return_value=mock_llm),
        patch(
            "asw.orchestrator.founder_review",
            side_effect=[
                FounderReviewResult(
                    action="answer_questions",
                    answers=[{"question": "Which database should we use?", "answer": "PostgreSQL"}],
                ),
                _APPROVE_REVIEW,
                _APPROVE_REVIEW,
                _APPROVE_REVIEW,
            ],
        ),
        patch("asw.orchestrator.founder_approve_devops_execution", return_value=_APPROVE_EXECUTION),
        patch("asw.orchestrator.subprocess.run", return_value=_SUCCESSFUL_DEVOPS_EXECUTION),
    ):
        result = run_pipeline(vision_path=vision, workdir=tmp_path)

    assert result == 0
    assert mock_llm.invoke.call_count == 14

    prd_content = (tmp_path / ".company" / "artifacts" / "prd.md").read_text(encoding="utf-8")
    assert "- Answer: PostgreSQL" in prd_content
    assert '"answer": "PostgreSQL"' in prd_content


def test_devops_execution_revision_requires_reapproved_proposal(tmp_path: Path) -> None:
    """A Founder revision request should regenerate the proposal before execution runs."""
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n\nBuild a CLI tool.\n")
    _setup_git_repo(tmp_path)

    mock_llm = _configure_mock_llm(
        MagicMock(),
        invoke_responses=[
            _CANNED_PRD,
            _CANNED_ARCH,
            _CANNED_EXECUTION_PLAN,
            _CANNED_ROSTER,
            _CANNED_ROLE,
            *_phase_preparation_responses(),
            _CANNED_DEVOPS_PROPOSAL,
            *_implementation_review_responses(),
        ],
    )

    with (
        patch("asw.orchestrator.get_backend", return_value=mock_llm),
        patch("asw.orchestrator.founder_review", return_value=_APPROVE_REVIEW),
        patch(
            "asw.orchestrator.founder_approve_devops_execution",
            side_effect=[
                ExecutionApprovalResult(action="revise", feedback="Tighten the safety summary."),
                _APPROVE_EXECUTION,
            ],
        ),
        patch("asw.orchestrator.subprocess.run", return_value=_SUCCESSFUL_DEVOPS_EXECUTION) as mock_execute,
    ):
        result = run_pipeline(
            vision_path=vision,
            workdir=tmp_path,
            options=PipelineRunOptions(execute_phase_setups=True),
        )

    assert result == 0
    assert mock_llm.invoke.call_count == 15
    bash_calls = [call for call in mock_execute.call_args_list if call.args and call.args[0][0] == "bash"]
    assert len(bash_calls) == 1
    assert any("Tighten the safety summary." in call.args[1] for call in mock_llm.invoke.call_args_list)


def test_founder_answers_across_phases_are_applied_locally(tmp_path: Path) -> None:
    """PRD, architecture, and execution-plan answers should all avoid extra LLM calls."""
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n\nBuild a CLI tool.\n")
    _setup_git_repo(tmp_path)

    mock_llm = _configure_mock_llm(
        MagicMock(),
        invoke_responses=[
            _CANNED_PRD_WITH_QUESTIONS,
            _CANNED_ARCH_WITH_QUESTIONS,
            _CANNED_EXECUTION_PLAN_WITH_QUESTIONS,
            _CANNED_ROSTER,
            _CANNED_ROLE,
            *_phase_preparation_responses(),
            *_implementation_review_responses(),
        ],
    )

    with (
        patch("asw.orchestrator.get_backend", return_value=mock_llm),
        patch(
            "asw.orchestrator.founder_review",
            side_effect=[
                FounderReviewResult(
                    action="answer_questions",
                    answers=[{"question": "Which database should we use?", "answer": "PostgreSQL"}],
                ),
                _APPROVE_REVIEW,
                FounderReviewResult(
                    action="answer_questions",
                    answers=[{"question": "Should we deploy locally first?", "answer": "Yes"}],
                ),
                _APPROVE_REVIEW,
                FounderReviewResult(
                    action="answer_questions",
                    answers=[{"question": "Should Phase 1 remain local-only?", "answer": "Yes"}],
                ),
                _APPROVE_REVIEW,
            ],
        ),
        patch("asw.orchestrator.founder_approve_devops_execution", return_value=_APPROVE_EXECUTION),
        patch("asw.orchestrator.subprocess.run", return_value=_SUCCESSFUL_DEVOPS_EXECUTION),
    ):
        result = run_pipeline(vision_path=vision, workdir=tmp_path)

    assert result == 0
    assert mock_llm.invoke.call_count == 14

    company = tmp_path / ".company" / "artifacts"
    assert '"answer": "Yes"' in (company / "architecture.json").read_text(encoding="utf-8")
    assert "Answer: Yes" in (company / "architecture.md").read_text(encoding="utf-8")
    assert '"answer": "Yes"' in (company / "execution_plan.json").read_text(encoding="utf-8")
    assert "Answer: Yes" in (company / "execution_plan.md").read_text(encoding="utf-8")


def test_request_more_questions_reruns_with_current_artifact_and_answers(tmp_path: Path) -> None:
    """Explicit follow-up question requests should rerun with artifact and answer context."""
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n\nBuild a CLI tool.\n")
    _setup_git_repo(tmp_path)

    mock_llm = _configure_mock_llm(
        MagicMock(),
        invoke_responses=[
            _CANNED_PRD_WITH_QUESTIONS,
            _CANNED_PRD,
            _CANNED_ARCH,
            _CANNED_EXECUTION_PLAN,
            _CANNED_ROSTER,
            _CANNED_ROLE,
            *_phase_preparation_responses(),
            *_implementation_review_responses(),
        ],
    )

    with (
        patch("asw.orchestrator.get_backend", return_value=mock_llm),
        patch(
            "asw.orchestrator.founder_review",
            side_effect=[
                FounderReviewResult(
                    action="answer_questions",
                    answers=[{"question": "Which database should we use?", "answer": "PostgreSQL"}],
                ),
                FounderReviewResult(
                    action="request_more_questions",
                    feedback="Ask one more round focused on deployment assumptions.",
                ),
                _APPROVE_REVIEW,
                _APPROVE_REVIEW,
                _APPROVE_REVIEW,
            ],
        ),
        patch("asw.orchestrator.founder_approve_devops_execution", return_value=_APPROVE_EXECUTION),
        patch("asw.orchestrator.subprocess.run", return_value=_SUCCESSFUL_DEVOPS_EXECUTION),
    ):
        result = run_pipeline(vision_path=vision, workdir=tmp_path)

    assert result == 0
    assert mock_llm.invoke.call_count == 15

    second_user_prompt = mock_llm.invoke.call_args_list[1].args[1]
    assert "### CURRENT_PRD" in second_user_prompt
    assert "### FOUNDER_ANSWERS" in second_user_prompt
    assert "PostgreSQL" in second_user_prompt


def test_architecture_request_more_questions_escalates_until_new_question(tmp_path: Path) -> None:
    """Architecture follow-up requests should not silently return with no new question."""
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n\nBuild a CLI tool.\n")
    _setup_git_repo(tmp_path)

    arch_without_new_question = """\
```json
{
    "project_name": "agenticorg",
    "tech_stack": {"language": "Python", "version": "3.14", "frameworks": [], "tools": ["argparse"]},
    "components": [{"name": "CLI", "responsibility": "Entry point", "interfaces": ["start"]}],
    "data_models": [{"name": "Vision", "fields": [{"name": "content", "type": "str"}]}],
    "api_contracts": [{"endpoint": "/start", "method": "CLI", "description": "Start pipeline"}],
    "deployment": {"platform": "DevContainer", "strategy": "local", "requirements": ["Docker"]},
    "founder_questions": [
        {"question": "Should we deploy locally first?", "answer": "Yes"}
    ]
}
```

```mermaid
graph TD
    CLI --> Orchestrator
```
"""

    arch_with_new_question = """\
```json
{
    "project_name": "agenticorg",
    "tech_stack": {"language": "Python", "version": "3.14", "frameworks": [], "tools": ["argparse"]},
    "components": [{"name": "CLI", "responsibility": "Entry point", "interfaces": ["start"]}],
    "data_models": [{"name": "Vision", "fields": [{"name": "content", "type": "str"}]}],
    "api_contracts": [{"endpoint": "/start", "method": "CLI", "description": "Start pipeline"}],
    "deployment": {"platform": "DevContainer", "strategy": "local", "requirements": ["Docker"]},
    "founder_questions": [
        {"question": "Should we deploy locally first?", "answer": "Yes"},
        {"question": "Do we need authentication in Phase 1?", "choices": ["Yes", "No"]}
    ]
}
```

```mermaid
graph TD
    CLI --> Orchestrator
```
"""

    mock_llm = _configure_mock_llm(
        MagicMock(),
        invoke_responses=[
            _CANNED_PRD,
            _CANNED_ARCH_WITH_QUESTIONS,
            arch_without_new_question,
            arch_with_new_question,
            _CANNED_EXECUTION_PLAN,
            _CANNED_ROSTER,
            _CANNED_ROLE,
            *_phase_preparation_responses(),
            *_implementation_review_responses(),
        ],
    )

    with (
        patch("asw.orchestrator.get_backend", return_value=mock_llm),
        patch(
            "asw.orchestrator.founder_review",
            side_effect=[
                _APPROVE_REVIEW,
                FounderReviewResult(
                    action="answer_questions",
                    answers=[{"question": "Should we deploy locally first?", "answer": "Yes"}],
                ),
                FounderReviewResult(
                    action="request_more_questions",
                    feedback="Ask one more unresolved architecture question.",
                ),
                FounderReviewResult(
                    action="answer_questions",
                    answers=[{"question": "Do we need authentication in Phase 1?", "answer": "No"}],
                ),
                _APPROVE_REVIEW,
                _APPROVE_REVIEW,
            ],
        ),
        patch("asw.orchestrator.founder_approve_devops_execution", return_value=_APPROVE_EXECUTION),
        patch("asw.orchestrator.subprocess.run", return_value=_SUCCESSFUL_DEVOPS_EXECUTION),
    ):
        result = run_pipeline(vision_path=vision, workdir=tmp_path)

    assert result == 0
    assert mock_llm.invoke.call_count == 16
    architecture_json = (tmp_path / ".company" / "artifacts" / "architecture.json").read_text(encoding="utf-8")
    assert '"question": "Do we need authentication in Phase 1?"' in architecture_json
    assert '"answer": "No"' in architecture_json


# ── Resume / restart tests ──────────────────────────────────────────────


def _setup_git_repo(tmp_path: Path) -> None:
    """Initialise a minimal git repo."""
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@test.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )


def _build_state(
    workdir: Path,
    phase_specs: dict[str, tuple[list[Path], list[Path]]],
    *,
    completed_at: str = "2026-01-01T00:00:00Z",
) -> dict:
    """Build a pipeline state document from input/output path snapshots."""
    state = new_pipeline_state()
    for phase, (inputs, outputs) in phase_specs.items():
        input_snapshot = snapshot_paths(workdir, inputs)
        output_snapshot = snapshot_paths(workdir, outputs)
        state["tracked_files"].update(input_snapshot)
        state["tracked_files"].update(output_snapshot)
        state["phases"][phase] = {
            "completed_at": completed_at,
            "inputs": input_snapshot,
            "outputs": output_snapshot,
        }
    return state


def _write_phase_preparation_artifacts(company: Path) -> PhaseArtifactPaths:
    """Write canned phase-preparation artifacts for the first phase."""
    paths = build_phase_artifact_paths(company, 0)
    paths.draft_path.parent.mkdir(parents=True, exist_ok=True)
    paths.script_path.parent.mkdir(parents=True, exist_ok=True)
    paths.draft_path.write_text(_CANNED_PHASE_DESIGN_DRAFT, encoding="utf-8")
    paths.feedback_path("Development Lead").write_text(_CANNED_PHASE_FEEDBACK_DEVELOPMENT_LEAD, encoding="utf-8")
    paths.feedback_path("DevOps Engineer").write_text(_CANNED_PHASE_FEEDBACK_DEVOPS, encoding="utf-8")
    paths.feedback_path("Python Backend Developer").write_text(_CANNED_PHASE_FEEDBACK_BACKEND, encoding="utf-8")
    paths.final_path.write_text(_CANNED_PHASE_DESIGN_FINAL, encoding="utf-8")
    write_phase_task_mapping(
        json.loads(_extract_json_block(_CANNED_PHASE_DESIGN_FINAL)),
        paths,
        phase_label="phase_1 - Local Validation",
    )
    paths.proposal_path.write_text(_CANNED_DEVOPS_PROPOSAL, encoding="utf-8")
    paths.summary_path.write_text(
        "# DevOps Setup Summary\n\n- Generated script path: `.devcontainer/phase_01_setup.sh`\n",
        encoding="utf-8",
    )
    paths.script_path.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\ntrap 'echo \"failed\" >&2' ERR\necho ready\n",
        encoding="utf-8",
    )
    return paths


def _phase_preparation_phase_specs(
    workdir: Path,
    company: Path,
    vision: Path,
) -> dict[str, tuple[list[Path], list[Path]]]:
    """Return state specs for the first phase-preparation slice."""
    ensure_validation_contract(company)
    paths = _write_phase_preparation_artifacts(company)
    validation_contract_json_path, _ = validation_contract_paths(company)
    design_inputs = [
        vision,
        company / "artifacts" / "prd.md",
        company / "artifacts" / "architecture.json",
        company / "artifacts" / "execution_plan.json",
        company / "artifacts" / "roster.json",
        validation_contract_json_path,
        company / "roles" / "development_lead.md",
        company / "roles" / "phase_feedback_reviewer.md",
        company / "roles" / "development_lead.md",
        company / "roles" / "devops_engineer.md",
        company / "roles" / "python_backend_developer.md",
    ]
    proposal_inputs = [
        paths.final_path,
        company / "roles" / "devops_engineer.md",
        workdir / ".devcontainer" / "post-create.sh",
        workdir / ".devcontainer" / "post-start.sh",
        workdir / ".devcontainer" / "devcontainer.json",
    ]
    return {
        "phase-loop:phase_1:design": (
            design_inputs,
            [
                paths.draft_path,
                paths.feedback_path("Development Lead"),
                paths.feedback_path("DevOps Engineer"),
                paths.feedback_path("Python Backend Developer"),
                paths.final_path,
                paths.task_mapping_json_path,
                paths.task_mapping_md_path,
            ],
        ),
        "phase-loop:phase_1:devops-proposal": (
            proposal_inputs,
            [paths.proposal_path, paths.summary_path, paths.script_path],
        ),
        "phase-loop:phase_1:devops-execution": ([paths.proposal_path, paths.summary_path, paths.script_path], []),
    }


def test_is_phase_done_returns_false_when_no_state() -> None:
    """_is_phase_done returns False with no state."""
    assert _is_phase_done(None, "prd", [], workdir=Path(".")) is False


def test_is_phase_done_returns_false_when_phase_missing(tmp_path: Path) -> None:
    """_is_phase_done returns False when phase is absent from state."""
    state = new_pipeline_state()
    assert _is_phase_done(state, "prd", [], workdir=tmp_path) is False


def test_is_phase_done_returns_false_when_artifact_missing(tmp_path: Path) -> None:
    """_is_phase_done returns False when artifact file doesn't exist."""
    missing = tmp_path / "nonexistent.md"
    state = _build_state(tmp_path, {"prd": ([tmp_path / "vision.md"], [missing])})
    assert _is_phase_done(state, "prd", [missing], workdir=tmp_path) is False


def test_is_phase_done_returns_true(tmp_path: Path) -> None:
    """_is_phase_done returns True when phase completed and artifacts exist."""
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n")
    artifact = tmp_path / "prd.md"
    artifact.write_text("content")
    state = _build_state(tmp_path, {"prd": ([vision], [artifact])})
    assert _is_phase_done(state, "prd", [artifact], workdir=tmp_path) is True


def test_resume_skips_completed_phases(tmp_path: Path) -> None:
    """Pipeline skips phases that are already completed with artifacts on disk."""
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n\nBuild a CLI tool.\n")
    _setup_git_repo(tmp_path)

    # Pre-populate .company/ with all artifacts and a state file.
    company = init_company(tmp_path)
    (company / "artifacts" / "prd.md").write_text(_CANNED_PRD, encoding="utf-8")
    (company / "artifacts" / "architecture.json").write_text(json.dumps({"project_name": "test"}), encoding="utf-8")
    (company / "artifacts" / "architecture.md").write_text("# Arch", encoding="utf-8")
    (company / "artifacts" / "execution_plan.json").write_text(
        _extract_json_block(_CANNED_EXECUTION_PLAN), encoding="utf-8"
    )
    (company / "artifacts" / "execution_plan.md").write_text("# Execution Plan", encoding="utf-8")
    (company / "artifacts" / "roster.json").write_text(_extract_json_block(_CANNED_ROSTER), encoding="utf-8")
    (company / "artifacts" / "roster.md").write_text("# Roster", encoding="utf-8")
    (company / "roles" / "python_backend_developer.md").write_text(_CANNED_ROLE, encoding="utf-8")
    phase_prep_specs = _phase_preparation_phase_specs(tmp_path, company, vision)

    state = _build_state(
        tmp_path,
        {
            "prd": ([vision, company / "roles" / "cpo.md"], [company / "artifacts" / "prd.md"]),
            "architecture": (
                [vision, company / "artifacts" / "prd.md", company / "roles" / "cto.md"],
                [company / "artifacts" / "architecture.json", company / "artifacts" / "architecture.md"],
            ),
            "execution_plan": (
                [
                    vision,
                    company / "artifacts" / "prd.md",
                    company / "artifacts" / "architecture.json",
                    company / "roles" / "vpe.md",
                    company / "templates" / "execution_plan_template.md",
                ],
                [company / "artifacts" / "execution_plan.json", company / "artifacts" / "execution_plan.md"],
            ),
            "roster": (
                [
                    company / "artifacts" / "architecture.json",
                    company / "artifacts" / "execution_plan.json",
                    company / "roles" / "hiring_manager.md",
                    company / "standards" / "python_guidelines.md",
                    company / "standards" / "ui_guidelines.md",
                ],
                [company / "artifacts" / "roster.json", company / "artifacts" / "roster.md"],
            ),
            "roles": (
                [
                    company / "artifacts" / "architecture.json",
                    company / "artifacts" / "execution_plan.json",
                    company / "artifacts" / "roster.json",
                    company / "roles" / "role_writer.md",
                    company / "templates" / "role_template.md",
                    company / "standards" / "python_guidelines.md",
                ],
                _expected_role_output_paths(company),
            ),
            **phase_prep_specs,
        },
    )
    write_pipeline_state(tmp_path, state)

    mock_llm = _configure_mock_llm(MagicMock(), invoke_responses=_implementation_review_responses())

    with (
        patch("asw.orchestrator.get_backend", return_value=mock_llm),
        patch("asw.orchestrator.founder_review", return_value=_APPROVE_REVIEW),
        patch("asw.orchestrator.founder_approve_devops_execution", return_value=_APPROVE_EXECUTION),
        patch("asw.orchestrator.subprocess.run", return_value=_SUCCESSFUL_DEVOPS_EXECUTION),
    ):
        result = run_pipeline(vision_path=vision, workdir=tmp_path)

    assert result == 0
    # Pre-implementation phases were skipped; only the three implementation reviews ran.
    assert mock_llm.invoke.call_count == 3


def test_phase_design_input_paths_include_validation_contract(tmp_path: Path) -> None:
    """Phase-design tracking should include the validation contract JSON artifact."""
    company = init_company(tmp_path)
    ensure_validation_contract(company)
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n\nBuild a CLI tool.\n", encoding="utf-8")
    exec_ctx = PipelineExecutionContext(
        state=new_pipeline_state(),
        company=company,
        vision_path=vision,
        vision_content=vision.read_text(encoding="utf-8"),
        llm=MagicMock(),
        options=PipelineRunOptions(no_commit=True),
    )
    team_entries = json.loads(_extract_json_block(_CANNED_ROSTER))["hired_agents"]
    input_paths = _phase_design_input_paths(exec_ctx, team_entries)
    validation_contract_json_path, _ = validation_contract_paths(company)

    assert validation_contract_json_path in input_paths


def test_run_phase_design_step_includes_validation_contract_context_and_persists_task_mapping(tmp_path: Path) -> None:
    """Phase-design generation should expose validation context and persist task-mapping artifacts."""
    company = init_company(tmp_path)
    ensure_validation_contract(company)
    (company / "roles" / "python_backend_developer.md").write_text(_CANNED_ROLE, encoding="utf-8")
    paths = build_phase_artifact_paths(company, 0)
    phase_data = json.loads(_extract_json_block(_CANNED_EXECUTION_PLAN))["phases"][0]
    captured_contexts: dict[str, dict] = {}

    def fake_agent_loop(_agent: MagicMock, context: dict, _lint_fn: MagicMock, label: str) -> str:
        captured_contexts[label] = context
        if label.endswith("Design Draft"):
            return _CANNED_PHASE_DESIGN_DRAFT
        if label.endswith("Feedback: Development Lead"):
            return _CANNED_PHASE_FEEDBACK_DEVELOPMENT_LEAD
        if label.endswith("Feedback: DevOps Engineer"):
            return _CANNED_PHASE_FEEDBACK_DEVOPS
        if label.endswith("Feedback: Python Backend Developer"):
            return _CANNED_PHASE_FEEDBACK_BACKEND
        if label.endswith("Design Final"):
            return _CANNED_PHASE_DESIGN_FINAL
        raise AssertionError(f"Unexpected label: {label}")

    with patch("asw.orchestrator._agent_loop", side_effect=fake_agent_loop):
        final = _run_phase_design_step(
            company,
            vision_content="# Vision\n",
            prd_content=_CANNED_PRD,
            architecture_json=_extract_json_block(_CANNED_ARCH),
            execution_plan_json=_extract_json_block(_CANNED_EXECUTION_PLAN),
            roster_json=_extract_json_block(_CANNED_ROSTER),
            phase_data=phase_data,
            phase_index=0,
            paths=paths,
            llm=MagicMock(),
        )

    draft_context = captured_contexts["phase_1 - Local Validation Design Draft"]
    assert final == _CANNED_PHASE_DESIGN_FINAL
    assert "validation_contract_json" in draft_context
    assert "validation_contract_markdown" in draft_context
    assert "change_policy" in draft_context["validation_contract_json"]
    assert paths.task_mapping_json_path.is_file()
    assert paths.task_mapping_md_path.is_file()


def test_run_or_skip_phase_design_step_backfills_task_mapping_without_llm_rerun(tmp_path: Path) -> None:
    """Missing derived task-mapping artifacts should be regenerated locally from saved final design."""
    company = init_company(tmp_path)
    ensure_validation_contract(company)
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n\nBuild a CLI tool.\n", encoding="utf-8")
    (company / "artifacts" / "prd.md").write_text(_CANNED_PRD, encoding="utf-8")
    (company / "artifacts" / "architecture.json").write_text(_extract_json_block(_CANNED_ARCH), encoding="utf-8")
    (company / "artifacts" / "execution_plan.json").write_text(
        _extract_json_block(_CANNED_EXECUTION_PLAN),
        encoding="utf-8",
    )
    roster_json = _extract_json_block(_CANNED_ROSTER)
    (company / "artifacts" / "roster.json").write_text(roster_json, encoding="utf-8")
    (company / "roles" / "python_backend_developer.md").write_text(_CANNED_ROLE, encoding="utf-8")
    paths = build_phase_artifact_paths(company, 0)
    paths.draft_path.parent.mkdir(parents=True, exist_ok=True)
    paths.draft_path.write_text(_CANNED_PHASE_DESIGN_DRAFT, encoding="utf-8")
    paths.feedback_path("Development Lead").write_text(_CANNED_PHASE_FEEDBACK_DEVELOPMENT_LEAD, encoding="utf-8")
    paths.feedback_path("DevOps Engineer").write_text(_CANNED_PHASE_FEEDBACK_DEVOPS, encoding="utf-8")
    paths.feedback_path("Python Backend Developer").write_text(_CANNED_PHASE_FEEDBACK_BACKEND, encoding="utf-8")
    paths.final_path.write_text(_CANNED_PHASE_DESIGN_FINAL, encoding="utf-8")

    phase_data = json.loads(_extract_json_block(_CANNED_EXECUTION_PLAN))["phases"][0]
    team_entries = json.loads(roster_json)["hired_agents"]
    validation_contract_json_path, _ = validation_contract_paths(company)
    current_design_inputs = _phase_design_input_paths(
        PipelineExecutionContext(
            state=new_pipeline_state(),
            company=company,
            vision_path=vision,
            vision_content=vision.read_text(encoding="utf-8"),
            llm=MagicMock(),
            options=PipelineRunOptions(no_commit=True),
        ),
        team_entries,
    )
    old_design_inputs = [path for path in current_design_inputs if path != validation_contract_json_path]
    old_output_paths = [
        paths.draft_path,
        paths.final_path,
        paths.feedback_path("Development Lead"),
        paths.feedback_path("DevOps Engineer"),
        paths.feedback_path("Python Backend Developer"),
    ]
    state = _build_state(
        tmp_path,
        {
            "phase-loop:phase_1:design": (
                old_design_inputs,
                old_output_paths,
            )
        },
    )
    exec_ctx = PipelineExecutionContext(
        state=state,
        company=company,
        vision_path=vision,
        vision_content=vision.read_text(encoding="utf-8"),
        llm=MagicMock(),
        options=PipelineRunOptions(no_commit=True),
    )

    with patch("asw.orchestrator._run_phase_design_step") as mock_run_phase_design_step:
        final = _run_or_skip_phase_design_step(
            exec_ctx,
            prd_content=_CANNED_PRD,
            architecture_json=_extract_json_block(_CANNED_ARCH),
            execution_plan_json=_extract_json_block(_CANNED_EXECUTION_PLAN),
            roster_json=roster_json,
            phase_data=phase_data,
            phase_index=0,
            paths=paths,
        )

    assert final == _CANNED_PHASE_DESIGN_FINAL
    assert paths.task_mapping_json_path.is_file()
    assert paths.task_mapping_md_path.is_file()
    mock_run_phase_design_step.assert_not_called()


def test_run_or_skip_phase_design_step_reruns_when_validation_contract_changes(tmp_path: Path) -> None:
    """Recorded validation-contract changes should invalidate the saved phase design."""
    company = init_company(tmp_path)
    contract = ensure_validation_contract(company)
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n\nBuild a CLI tool.\n", encoding="utf-8")
    (company / "artifacts" / "prd.md").write_text(_CANNED_PRD, encoding="utf-8")
    (company / "artifacts" / "architecture.json").write_text(_extract_json_block(_CANNED_ARCH), encoding="utf-8")
    (company / "artifacts" / "execution_plan.json").write_text(
        _extract_json_block(_CANNED_EXECUTION_PLAN),
        encoding="utf-8",
    )
    roster_json = _extract_json_block(_CANNED_ROSTER)
    (company / "artifacts" / "roster.json").write_text(roster_json, encoding="utf-8")
    (company / "roles" / "python_backend_developer.md").write_text(_CANNED_ROLE, encoding="utf-8")
    paths = _write_phase_preparation_artifacts(company)

    phase_data = json.loads(_extract_json_block(_CANNED_EXECUTION_PLAN))["phases"][0]
    team_entries = json.loads(roster_json)["hired_agents"]
    exec_ctx = PipelineExecutionContext(
        state=new_pipeline_state(),
        company=company,
        vision_path=vision,
        vision_content=vision.read_text(encoding="utf-8"),
        llm=MagicMock(),
        options=PipelineRunOptions(no_commit=True),
    )
    state = _build_state(
        tmp_path,
        {
            "phase-loop:phase_1:design": (
                _phase_design_input_paths(exec_ctx, team_entries),
                [
                    paths.draft_path,
                    paths.final_path,
                    paths.feedback_path("Development Lead"),
                    paths.feedback_path("DevOps Engineer"),
                    paths.feedback_path("Python Backend Developer"),
                    paths.task_mapping_json_path,
                    paths.task_mapping_md_path,
                ],
            )
        },
    )
    exec_ctx.state = state

    validation_contract_json_path, _ = validation_contract_paths(company)
    contract["summary"] = "Validation coverage changed after the saved phase design."
    validation_contract_json_path.write_text(json.dumps(contract, indent=2) + "\n", encoding="utf-8")

    with (
        patch(
            "asw.orchestrator._run_phase_design_step",
            return_value=_CANNED_PHASE_DESIGN_FINAL,
        ) as mock_run_phase_design,
        patch("builtins.input", return_value="r"),
    ):
        final = _run_or_skip_phase_design_step(
            exec_ctx,
            prd_content=_CANNED_PRD,
            architecture_json=_extract_json_block(_CANNED_ARCH),
            execution_plan_json=_extract_json_block(_CANNED_EXECUTION_PLAN),
            roster_json=roster_json,
            phase_data=phase_data,
            phase_index=0,
            paths=paths,
        )

    assert final == _CANNED_PHASE_DESIGN_FINAL
    mock_run_phase_design.assert_called_once()


def test_resume_reruns_missing_artifact(tmp_path: Path) -> None:
    """If state says 'prd' done but prd.md is missing, phase reruns."""
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n\nBuild a CLI tool.\n")
    _setup_git_repo(tmp_path)

    company = init_company(tmp_path)
    # State says prd done, but NO prd.md on disk.
    state = _build_state(
        tmp_path,
        {
            "prd": ([vision, company / "roles" / "cpo.md"], [company / "artifacts" / "prd.md"]),
        },
    )
    write_pipeline_state(tmp_path, state)

    mock_llm = _make_mock_llm()

    with (
        patch("asw.orchestrator.get_backend", return_value=mock_llm),
        patch("asw.orchestrator.founder_review", return_value=_APPROVE_REVIEW),
        patch("asw.orchestrator.founder_approve_devops_execution", return_value=_APPROVE_EXECUTION),
        patch("asw.orchestrator.subprocess.run", return_value=_SUCCESSFUL_DEVOPS_EXECUTION),
    ):
        result = run_pipeline(vision_path=vision, workdir=tmp_path)

    assert result == 0
    # LLM was called because prd.md was missing, triggering all downstream phases.
    assert mock_llm.invoke.call_count == 14  # All phases ran plus three implementation reviews.
    assert (company / "artifacts" / "prd.md").is_file()


def test_prd_commit_failure_is_retried_without_rerunning_prd(tmp_path: Path) -> None:
    """A failed PRD commit should be retried on rerun without spending another PRD LLM call."""
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n\nBuild a CLI tool.\n")
    _setup_git_repo(tmp_path)

    mock_llm = _make_mock_llm()

    with (
        patch("asw.orchestrator.get_backend", return_value=mock_llm),
        patch("asw.orchestrator.founder_review", return_value=_APPROVE_REVIEW),
        patch("asw.orchestrator.founder_approve_devops_execution", return_value=_APPROVE_EXECUTION),
        patch("asw.orchestrator.subprocess.run", return_value=_SUCCESSFUL_DEVOPS_EXECUTION),
        patch(
            "asw.orchestrator.commit_state",
            side_effect=[GitError("commit failed"), "", "", "", "", "", "", ""],
        ),
    ):
        first_result = run_pipeline(vision_path=vision, workdir=tmp_path)
        assert first_result == 1

        state = read_pipeline_state(tmp_path)
        assert state is not None
        assert "prd" in state["phases"]
        assert "commit:prd-generation" not in state["phases"]

        second_result = run_pipeline(vision_path=vision, workdir=tmp_path)

    assert second_result == 0
    assert mock_llm.invoke.call_count == 14

    state = read_pipeline_state(tmp_path)
    assert state is not None
    assert "commit:prd-generation" in state["phases"]


def test_hiring_commit_failure_is_retried_without_rerunning_roles(tmp_path: Path) -> None:
    """A failed final hiring commit should be retried on rerun without new LLM calls."""
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n\nBuild a CLI tool.\n")
    _setup_git_repo(tmp_path)

    mock_llm = _make_mock_llm()

    with (
        patch("asw.orchestrator.get_backend", return_value=mock_llm),
        patch("asw.orchestrator.founder_review", return_value=_APPROVE_REVIEW),
        patch("asw.orchestrator.founder_approve_devops_execution", return_value=_APPROVE_EXECUTION),
        patch("asw.orchestrator.subprocess.run", return_value=_SUCCESSFUL_DEVOPS_EXECUTION),
        patch(
            "asw.orchestrator.commit_state",
            side_effect=["", "", "", GitError("commit failed"), "", "", "", ""],
        ),
    ):
        first_result = run_pipeline(vision_path=vision, workdir=tmp_path)
        assert first_result == 1

        state = read_pipeline_state(tmp_path)
        assert state is not None
        assert "roles" in state["phases"]
        assert "commit:hiring" not in state["phases"]

        second_result = run_pipeline(vision_path=vision, workdir=tmp_path)

    assert second_result == 0
    assert mock_llm.invoke.call_count == 14

    state = read_pipeline_state(tmp_path)
    assert state is not None
    assert "commit:hiring" in state["phases"]


def test_resume_reruns_missing_role_file(tmp_path: Path) -> None:
    """If a generated role file is missing, role generation should rerun."""
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n\nBuild a CLI tool.\n")

    company = init_company(tmp_path)
    (company / "artifacts" / "prd.md").write_text(_CANNED_PRD, encoding="utf-8")
    (company / "artifacts" / "architecture.json").write_text(json.dumps({"project_name": "test"}), encoding="utf-8")
    (company / "artifacts" / "architecture.md").write_text("# Arch", encoding="utf-8")
    (company / "artifacts" / "execution_plan.json").write_text(
        _extract_json_block(_CANNED_EXECUTION_PLAN), encoding="utf-8"
    )
    (company / "artifacts" / "execution_plan.md").write_text("# Execution Plan", encoding="utf-8")
    (company / "artifacts" / "roster.json").write_text(_extract_json_block(_CANNED_ROSTER), encoding="utf-8")
    (company / "artifacts" / "roster.md").write_text("# Roster", encoding="utf-8")
    state = _build_state(
        tmp_path,
        {
            "prd": ([vision, company / "roles" / "cpo.md"], [company / "artifacts" / "prd.md"]),
            "architecture": (
                [vision, company / "artifacts" / "prd.md", company / "roles" / "cto.md"],
                [company / "artifacts" / "architecture.json", company / "artifacts" / "architecture.md"],
            ),
            "execution_plan": (
                [
                    vision,
                    company / "artifacts" / "prd.md",
                    company / "artifacts" / "architecture.json",
                    company / "roles" / "vpe.md",
                    company / "templates" / "execution_plan_template.md",
                ],
                [company / "artifacts" / "execution_plan.json", company / "artifacts" / "execution_plan.md"],
            ),
            "roster": (
                [
                    company / "artifacts" / "architecture.json",
                    company / "artifacts" / "execution_plan.json",
                    company / "roles" / "hiring_manager.md",
                    company / "standards" / "python_guidelines.md",
                    company / "standards" / "ui_guidelines.md",
                ],
                [company / "artifacts" / "roster.json", company / "artifacts" / "roster.md"],
            ),
            "roles": (
                [
                    company / "artifacts" / "architecture.json",
                    company / "artifacts" / "execution_plan.json",
                    company / "artifacts" / "roster.json",
                    company / "roles" / "role_writer.md",
                    company / "templates" / "role_template.md",
                    company / "standards" / "python_guidelines.md",
                ],
                _expected_role_output_paths(company),
            ),
        },
    )
    write_pipeline_state(tmp_path, state)

    mock_llm = _configure_mock_llm(
        MagicMock(),
        invoke_responses=[_CANNED_ROLE, *_phase_preparation_responses(), *_implementation_review_responses()],
    )

    with (
        patch("asw.orchestrator.get_backend", return_value=mock_llm),
        patch("asw.orchestrator.founder_approve_devops_execution", return_value=_APPROVE_EXECUTION),
        patch("asw.orchestrator.subprocess.run", return_value=_SUCCESSFUL_DEVOPS_EXECUTION),
    ):
        result = run_pipeline(vision_path=vision, workdir=tmp_path, options=PipelineRunOptions(no_commit=True))

    assert result == 0
    assert mock_llm.invoke.call_count == 10
    assert (company / "roles" / "python_backend_developer.md").is_file()


def test_restart_flag_wipes_company(tmp_path: Path) -> None:
    """--restart deletes .company/ and runs all phases fresh."""
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n\nBuild a CLI tool.\n")
    _setup_git_repo(tmp_path)

    # Pre-populate state as if all phases completed.
    company = init_company(tmp_path)
    state = _build_state(
        tmp_path,
        {
            "prd": ([vision, tmp_path / ".company" / "roles" / "cpo.md"], [company / "artifacts" / "prd.md"]),
            "architecture": (
                [vision, company / "artifacts" / "prd.md", tmp_path / ".company" / "roles" / "cto.md"],
                [company / "artifacts" / "architecture.json", company / "artifacts" / "architecture.md"],
            ),
            "execution_plan": (
                [
                    vision,
                    company / "artifacts" / "prd.md",
                    company / "artifacts" / "architecture.json",
                    tmp_path / ".company" / "roles" / "vpe.md",
                    tmp_path / ".company" / "templates" / "execution_plan_template.md",
                ],
                [company / "artifacts" / "execution_plan.json", company / "artifacts" / "execution_plan.md"],
            ),
            "roster": (
                [
                    company / "artifacts" / "architecture.json",
                    company / "artifacts" / "execution_plan.json",
                    tmp_path / ".company" / "roles" / "hiring_manager.md",
                    tmp_path / ".company" / "standards" / "python_guidelines.md",
                    tmp_path / ".company" / "standards" / "ui_guidelines.md",
                ],
                [company / "artifacts" / "roster.json", company / "artifacts" / "roster.md"],
            ),
            "roles": (
                [
                    company / "artifacts" / "architecture.json",
                    company / "artifacts" / "execution_plan.json",
                    company / "artifacts" / "roster.json",
                    tmp_path / ".company" / "roles" / "role_writer.md",
                    tmp_path / ".company" / "templates" / "role_template.md",
                    tmp_path / ".company" / "standards" / "python_guidelines.md",
                ],
                _expected_role_output_paths(company),
            ),
        },
    )
    write_pipeline_state(tmp_path, state)

    mock_llm = _make_mock_llm()

    with (
        patch("asw.orchestrator.get_backend", return_value=mock_llm),
        patch("asw.orchestrator.founder_review", return_value=_APPROVE_REVIEW),
        patch("asw.orchestrator.founder_approve_devops_execution", return_value=_APPROVE_EXECUTION),
        patch("asw.orchestrator.subprocess.run", return_value=_SUCCESSFUL_DEVOPS_EXECUTION),
    ):
        result = run_pipeline(vision_path=vision, workdir=tmp_path, options=PipelineRunOptions(restart=True))

    assert result == 0
    # All planning and phase-preparation LLM calls should have been made (no skipping).
    assert mock_llm.invoke.call_count == 14


def test_tracked_input_change_continue(tmp_path: Path) -> None:
    """Changed tracked inputs can be continued with explicitly."""
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision v1\n\nOriginal vision.\n")
    _setup_git_repo(tmp_path)

    company = init_company(tmp_path)
    (company / "artifacts" / "prd.md").write_text(_CANNED_PRD, encoding="utf-8")
    state = _build_state(
        tmp_path, {"prd": ([vision, company / "roles" / "cpo.md"], [company / "artifacts" / "prd.md"])}
    )
    write_pipeline_state(tmp_path, state)

    vision.write_text("# Vision v2\n\nUpdated vision.\n")

    mock_llm = _configure_mock_llm(
        MagicMock(),
        invoke_responses=[
            _CANNED_ARCH,
            _CANNED_EXECUTION_PLAN,
            _CANNED_ROSTER,
            _CANNED_ROLE,
            *_phase_preparation_responses(),
            *_implementation_review_responses(),
        ],
    )

    with (
        patch("asw.orchestrator.get_backend", return_value=mock_llm),
        patch("asw.orchestrator.founder_review", return_value=_APPROVE_REVIEW),
        patch("asw.orchestrator.founder_approve_devops_execution", return_value=_APPROVE_EXECUTION),
        patch("asw.orchestrator.subprocess.run", return_value=_SUCCESSFUL_DEVOPS_EXECUTION),
        patch("builtins.input", return_value="c"),
    ):
        result = run_pipeline(vision_path=vision, workdir=tmp_path)

    assert result == 0
    assert mock_llm.invoke.call_count == 13


def test_tracked_input_change_restart(tmp_path: Path) -> None:
    """Changed tracked inputs can trigger a full restart from the prompt."""
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision v1\n\nOriginal vision.\n")
    _setup_git_repo(tmp_path)

    company = init_company(tmp_path)
    (company / "artifacts" / "prd.md").write_text(_CANNED_PRD, encoding="utf-8")
    state = _build_state(
        tmp_path, {"prd": ([vision, company / "roles" / "cpo.md"], [company / "artifacts" / "prd.md"])}
    )
    write_pipeline_state(tmp_path, state)

    vision.write_text("# Vision v2\n\nUpdated vision.\n")

    mock_llm = _make_mock_llm()

    with (
        patch("asw.orchestrator.get_backend", return_value=mock_llm),
        patch("asw.orchestrator.founder_review", return_value=_APPROVE_REVIEW),
        patch("asw.orchestrator.founder_approve_devops_execution", return_value=_APPROVE_EXECUTION),
        patch("asw.orchestrator.subprocess.run", return_value=_SUCCESSFUL_DEVOPS_EXECUTION),
        patch("builtins.input", return_value="s"),
    ):
        result = run_pipeline(vision_path=vision, workdir=tmp_path)

    assert result == 0
    assert mock_llm.invoke.call_count == 14
