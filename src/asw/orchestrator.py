"""Pipeline orchestrator – the main SDLC loop."""

# pylint: disable=too-many-lines

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from asw.agents.base import Agent
from asw.company import (
    clear_company,
    hash_file,
    init_company,
    mark_phase_complete,
    new_pipeline_state,
    read_pipeline_state,
    snapshot_paths,
    write_failed_artifact,
    write_pipeline_state,
)
from asw.core_roles import MANDATORY_CORE_ROLE_FILENAMES
from asw.execution_plan import _lint_execution_plan, _write_execution_plan
from asw.founder_questions import (
    _apply_founder_answers_to_content,
    _apply_founder_answers_to_prd,
    _extract_answered_founder_questions,
    _extract_founder_question_items,
    _extract_founder_questions,
    _render_founder_question_section,
)
from asw.gates import FounderReviewResult, founder_approve_devops_execution, founder_review
from asw.git import GitError, commit_state, is_git_repo, repo_root, worktree_changed_paths
from asw.hiring import _expected_role_paths, _lint_roster, _write_roster
from asw.linters.json_lint import validate_architecture
from asw.linters.markdown import validate_checklist, validate_mermaid, validate_sections
from asw.llm.backend import LLMBackend, get_backend
from asw.llm.errors import LLMInvocationError
from asw.phase_implementation import (
    PhaseImplementationTurn,
    build_development_lead_review_request,
    build_implementation_execute_request,
    build_implementation_plan_request,
    lint_development_lead_review_json,
    next_phase_implementation_turn,
    render_phase_implementation_turn_summary,
)
from asw.phase_preparation import (
    PhaseArtifactPaths,
    build_phase_artifact_paths,
    extract_fenced_code_block,
    extract_markdown_list_items,
    find_tracked_file_mutations,
    lint_devops_proposal,
    lint_phase_design,
    lint_phase_feedback,
    render_setup_summary,
    snapshot_tracked_repo_files,
)
from asw.phase_tasks import lint_phase_task_mapping_json, write_phase_task_mapping
from asw.pipeline import PipelineExecutionContext, PipelineRunOptions, string_checksum_prefix
from asw.validation_contract import (
    ensure_validation_contract,
    load_validation_contract,
    render_validation_contract_markdown,
    validation_contract_paths,
)
from asw.validation_runner import render_validation_report_markdown, run_validation_contract

_MAX_RETRIES = 2
_REQUEST_MORE_QUESTIONS_FEEDBACK = (
    "Review the current artifact, preserve all founder decisions already captured, and ask at least one new "
    "founder question whenever meaningful unresolved issues remain."
)
_REQUEST_MORE_QUESTIONS_ESCALATION = (
    "The founder explicitly requested another question round. Return at least one new unresolved founder "
    "question that is not already answered when any meaningful ambiguity remains."
)
_PHASE_SETUP_EXECUTION_DEFERRED_REASON = (
    "Phase setup execution is deferred until the implementation loops are available."
)

logger = logging.getLogger("asw.orchestrator")
_console = Console(stderr=True)


class PipelineRestartRequested(RuntimeError):
    """Raised when the founder chooses to restart from scratch mid-run."""


@dataclass(frozen=True)
class PhaseStatus:  # pylint: disable=too-many-instance-attributes
    """Snapshot of a phase's recorded and current tracked files."""

    phase: str
    completed_at: str | None
    recorded_inputs: dict[str, str | None]
    recorded_outputs: dict[str, str | None]
    current_inputs: dict[str, str | None]
    current_outputs: dict[str, str | None]
    changed_inputs: list[str]
    changed_outputs: list[str]
    missing_outputs: list[str]

    @property
    def has_record(self) -> bool:
        """Return whether the phase has a stored completion record."""
        return self.completed_at is not None

    @property
    def is_current(self) -> bool:
        """Return whether the stored phase snapshot still matches the worktree."""
        return self.has_record and not self.changed_inputs and not self.changed_outputs and not self.missing_outputs


@dataclass(frozen=True)
class ImplementationTurnStepStatus:
    """Persisted status for one implementation-turn step."""

    step: str
    phase_status: PhaseStatus
    metadata: dict[str, object]

    @property
    def has_record(self) -> bool:
        """Return whether the step has a stored completion record."""
        return self.phase_status.has_record

    @property
    def is_current(self) -> bool:
        """Return whether the stored step snapshot is still current."""
        return self.phase_status.is_current

    @property
    def attempt(self) -> int | None:
        """Return the recorded step attempt number."""
        attempt = self.metadata.get("attempt")
        return attempt if isinstance(attempt, int) and attempt > 0 else None


@dataclass(frozen=True)
class ImplementationTurnResumePlan:
    """How one implementation turn should continue or rerun."""

    action: str
    attempt: int
    start_step: str
    feedback: str | None = None
    approved_paths: list[str] | None = None
    baseline_changed_paths: list[str] | None = None


def _format_paths(paths: list[str]) -> str:
    """Format tracked file paths for terminal output."""
    return ", ".join(paths) if paths else "(none)"


def _phase_record(state: dict | None, phase: str) -> dict:
    """Return the stored phase record for *phase*, or an empty dict."""
    if state is None:
        return {}
    phases = state.get("phases", {})
    if not isinstance(phases, dict):
        return {}
    record = phases.get(phase, {})
    return record if isinstance(record, dict) else {}


def _evaluate_phase_status(
    exec_ctx: PipelineExecutionContext,
    phase: str,
    *,
    input_paths: list[Path],
    output_paths: list[Path],
) -> PhaseStatus:
    """Return the current status of *phase* against the tracked file hashes."""
    record = _phase_record(exec_ctx.state, phase)
    completed_at = record.get("completed_at") if isinstance(record.get("completed_at"), str) else None
    recorded_inputs = record.get("inputs", {}) if isinstance(record.get("inputs"), dict) else {}
    recorded_outputs = record.get("outputs", {}) if isinstance(record.get("outputs"), dict) else {}

    current_inputs = snapshot_paths(exec_ctx.workdir, input_paths)
    current_outputs = snapshot_paths(exec_ctx.workdir, output_paths)

    if completed_at is None:
        return PhaseStatus(
            phase=phase,
            completed_at=None,
            recorded_inputs={},
            recorded_outputs={},
            current_inputs=current_inputs,
            current_outputs=current_outputs,
            changed_inputs=[],
            changed_outputs=[],
            missing_outputs=[],
        )

    input_keys = set(recorded_inputs) | set(current_inputs)
    output_keys = set(recorded_outputs) | set(current_outputs)
    changed_inputs = sorted(path for path in input_keys if recorded_inputs.get(path) != current_inputs.get(path))
    changed_outputs = sorted(path for path in output_keys if recorded_outputs.get(path) != current_outputs.get(path))
    missing_outputs = sorted(path for path, digest in current_outputs.items() if digest is None)

    return PhaseStatus(
        phase=phase,
        completed_at=completed_at,
        recorded_inputs=recorded_inputs,
        recorded_outputs=recorded_outputs,
        current_inputs=current_inputs,
        current_outputs=current_outputs,
        changed_inputs=changed_inputs,
        changed_outputs=changed_outputs,
        missing_outputs=missing_outputs,
    )


def _print_skip_message(label: str, status: PhaseStatus) -> None:
    """Print an informative skip message for a completed phase."""
    print(f"\n↩ Skipping {label} phase (completed {status.completed_at})")
    if status.recorded_inputs:
        print(f"   Tracked inputs: {_format_paths(sorted(status.recorded_inputs))}")
    if status.recorded_outputs:
        print(f"   Verified outputs: {_format_paths(sorted(status.recorded_outputs))}")


def _print_rerun_reason(label: str, status: PhaseStatus) -> None:
    """Print why a completed phase can no longer be skipped."""
    print(f"\n↻ Rerunning {label} phase because the saved snapshot is no longer current.")
    if status.changed_inputs:
        print(f"   Changed inputs: {_format_paths(status.changed_inputs)}")
    if status.missing_outputs:
        print(f"   Missing outputs: {_format_paths(status.missing_outputs)}")
    elif status.changed_outputs:
        print(f"   Changed outputs: {_format_paths(status.changed_outputs)}")


def _prompt_phase_invalidation(label: str, status: PhaseStatus) -> str:
    """Ask whether to continue, rerun, or restart after tracked changes."""
    print(f"\n⚠  {label} phase inputs changed since the last completed snapshot.")
    print(f"   Changed inputs: {_format_paths(status.changed_inputs)}")
    print("   The saved artifacts may now be stale.")
    while True:
        choice = input("   [C]ontinue with saved artifacts, [R]erun this phase, or re[S]tart from scratch? ")
        choice = choice.strip().lower()
        if choice in ("c", "continue"):
            return "continue"
        if choice in ("r", "rerun"):
            return "rerun"
        if choice in ("s", "restart"):
            return "restart"
        print("   Please enter 'C', 'R', or 'S'.")


def _commit_phase_name(phase_name: str) -> str:
    """Return the pipeline-state marker name for a git commit phase."""
    return f"commit:{phase_name}"


def _clear_phase_marker(workdir: Path, state: dict, phase: str) -> None:
    """Remove a completion marker from pipeline state when it is no longer valid."""
    phases = state.get("phases", {})
    if phase in phases:
        del phases[phase]
        write_pipeline_state(workdir, state)


def _clear_phase_markers(workdir: Path, state: dict, phases: list[str]) -> None:
    """Remove multiple completion markers from pipeline state."""
    stored_phases = state.get("phases", {})
    if not isinstance(stored_phases, dict):
        return

    changed = False
    for phase in phases:
        if phase in stored_phases:
            del stored_phases[phase]
            changed = True

    if changed:
        write_pipeline_state(workdir, state)


def _ensure_commit_complete(exec_ctx: PipelineExecutionContext, phase_name: str) -> int | None:
    """Run a commit phase once and record its completion separately from generation."""
    commit_phase = _commit_phase_name(phase_name)
    if _is_phase_done(exec_ctx.state, commit_phase, [], workdir=exec_ctx.workdir):
        return None

    err = _try_commit(
        exec_ctx.workdir,
        phase_name,
        exec_ctx.options.no_commit,
        stage_all=exec_ctx.options.stage_all,
    )
    if err is None:
        mark_phase_complete(exec_ctx.workdir, exec_ctx.state, commit_phase, input_paths=[], output_paths=[])
    return err


def _agent_company(agent: Agent) -> Path | None:
    """Infer the company directory for *agent*, if available."""
    role_file = getattr(agent, "role_file", None)
    if isinstance(role_file, Path) and role_file.parent.name == "roles":
        return role_file.parent.parent
    return None


def _safe_join(items: list[str] | str) -> str:
    """Safely join a list of strings, or return the string if not a list."""
    if isinstance(items, str):
        return items
    return ", ".join(items)


def _render_tech_stack(data: dict) -> list[str]:
    """Render the tech-stack section of the architecture Markdown."""
    ts = data.get("tech_stack", {})
    frameworks = _safe_join(ts.get("frameworks", [])) or "None"
    tools = _safe_join(ts.get("tools", [])) or "None"
    return [
        "### Tech Stack",
        f"- **Language:** {ts.get('language', 'N/A')} ({ts.get('version', 'N/A')})",
        f"- **Frameworks:** {frameworks}",
        f"- **Tools:** {tools}",
        "",
    ]


def _render_components(data: dict) -> list[str]:
    """Render the components table of the architecture Markdown."""
    lines = [
        "### Components",
        "| Name | Responsibility | Interfaces |",
        "| --- | --- | --- |",
    ]
    for comp in data.get("components", []):
        iface = _safe_join(comp.get("interfaces", [])) or "None"
        lines.append(f"| {comp.get('name', 'N/A')} | {comp.get('responsibility', 'N/A')} | {iface} |")
    lines.append("")
    return lines


def _render_data_models(data: dict) -> list[str]:
    """Render the data-models section of the architecture Markdown."""
    lines: list[str] = ["### Data Models"]
    for model in data.get("data_models", []):
        lines.append(f"#### {model.get('name', 'N/A')}")
        lines.extend(["| Field | Type |", "| --- | --- |"])
        for field in model.get("fields", []):
            lines.append(f"| {field.get('name', 'N/A')} | {field.get('type', 'N/A')} |")
        lines.append("")
    return lines


def _render_api_contracts(data: dict) -> list[str]:
    """Render the API-contracts table of the architecture Markdown."""
    lines = [
        "### API Contracts",
        "| Endpoint | Method | Description |",
        "| --- | --- | --- |",
    ]
    for api in data.get("api_contracts", []):
        lines.append(f"| {api.get('endpoint', 'N/A')} | {api.get('method', 'N/A')} | {api.get('description', 'N/A')} |")
    lines.append("")
    return lines


def _render_deployment(data: dict) -> list[str]:
    """Render the deployment section of the architecture Markdown."""
    dep = data.get("deployment", {})
    reqs = _safe_join(dep.get("requirements", [])) or "None"
    return [
        "### Deployment",
        f"- **Platform:** {dep.get('platform', 'N/A')}",
        f"- **Strategy:** {dep.get('strategy', 'N/A')}",
        f"- **Requirements:** {reqs}",
        "",
    ]


def _render_architecture_markdown(json_str: str, mermaid_str: str) -> str:
    """Render a human-readable Markdown from architecture JSON and Mermaid."""
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        return (
            "# System Architecture\n\n"
            "> **Warning:** Technical specification could not be parsed.\n\n"
            f"```mermaid\n{mermaid_str}\n```"
        )

    lines = [
        "# System Architecture",
        "",
        "> **Source of Truth:** The technical specification for this architecture is stored in `architecture.json`.",
        "",
        "## Visual Overview",
        f"```mermaid\n{mermaid_str}\n```",
        "",
        f"## Project: {data.get('project_name', 'N/A')}",
        "",
    ]
    lines.extend(_render_tech_stack(data))
    lines.extend(_render_components(data))
    lines.extend(_render_data_models(data))
    lines.extend(_render_api_contracts(data))
    lines.extend(_render_deployment(data))
    founder_questions = data.get("founder_questions", [])
    if isinstance(founder_questions, list) and founder_questions:
        lines.extend(_render_founder_question_section(founder_questions, heading="### Founder Input"))

    return "\n".join(lines)


_PRD_REQUIRED_SECTIONS = [
    "Executive Summary",
    "Goals & Success Metrics",
    "Target Users",
    "Functional Requirements",
    "Non-Functional Requirements",
    "User Stories",
    "Acceptance Criteria Checklist",
    "System Overview Diagram",
    "Risks & Mitigations",
    "Open Questions",
]


def _lint_prd(content: str) -> list[str]:
    """Run all mechanical linters on a PRD document."""
    errors: list[str] = []
    errors.extend(validate_sections(content, _PRD_REQUIRED_SECTIONS))
    errors.extend(validate_checklist(content))
    errors.extend(validate_mermaid(content))
    logger.debug("PRD lint result: %d error(s)", len(errors))
    for err in errors:
        logger.debug("  PRD lint error: %s", err)
    return errors


def _extract_json_block(content: str) -> str | None:
    """Extract the first fenced JSON code block from *content*."""
    match = re.search(r"```json\s*(.*?)\s*```", content, re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else None


def _build_revision_context(base_context: dict[str, str], artifact_key: str, artifact_content: str) -> dict[str, str]:
    """Build rerun context including the current artifact and founder answers."""
    context = dict(base_context)
    context[artifact_key] = artifact_content

    answered = _extract_answered_founder_questions(artifact_content)
    if answered:
        context["founder_answers"] = json.dumps(answered, indent=2)

    return context


def _review_feedback(review: FounderReviewResult) -> str | None:
    """Return the feedback string that should accompany a rerun."""
    if review.action == "request_more_questions":
        return review.feedback or _REQUEST_MORE_QUESTIONS_FEEDBACK
    return review.feedback


def _has_new_unanswered_questions(previous_items: list[dict], updated_content: str) -> bool:
    """Return whether *updated_content* contains a new unresolved founder question."""
    previous_questions = {
        item["question"] for item in previous_items if isinstance(item, dict) and isinstance(item.get("question"), str)
    }
    current_questions = _extract_founder_questions(updated_content) or []
    return any(
        isinstance(item.get("question"), str) and item["question"] not in previous_questions
        for item in current_questions
    )


def _extract_mermaid_block(content: str) -> str | None:
    """Extract the first fenced Mermaid code block from *content*."""
    match = re.search(r"```mermaid\s*\n(.*?)```", content, re.DOTALL)
    return match.group(1).strip() if match else None


def _lint_architecture(content: str) -> tuple[list[str], str | None, str | None]:
    """Lint CTO output.  Returns ``(errors, json_str, mermaid_str)``."""
    errors: list[str] = []

    json_block = _extract_json_block(content)
    mermaid_block = _extract_mermaid_block(content)

    if json_block is None:
        errors.append("No fenced ```json``` code block found in CTO output.")
    else:
        errors.extend(validate_architecture(json_block))

    if mermaid_block is None:
        errors.append("No fenced ```mermaid``` code block found in CTO output.")

    logger.debug("Architecture lint result: %d error(s)", len(errors))
    for err in errors:
        logger.debug("  Architecture lint error: %s", err)
    return errors, json_block, mermaid_block


def _supports_live_status() -> bool:
    """Return whether the current terminal supports live Rich status updates."""
    return _console.is_terminal and not _console.is_dumb_terminal


def _agent_display_name(agent_name: str) -> str:
    """Return the user-facing display name for an agent."""
    return agent_name.removesuffix(" Feedback")


def _agent_status_message(agent_name: str, phase_name: str) -> str:
    """Return a role-aware progress message for an agent invocation."""
    display_name = _agent_display_name(agent_name)

    if phase_name == "PRD":
        return f"{display_name} drafting the PRD"
    if phase_name == "Architecture":
        return f"{display_name} designing the architecture"
    if phase_name == "Execution Plan":
        return f"{display_name} designing the execution plan"
    if phase_name == "Roster":
        return f"{display_name} assembling the roster"
    if phase_name.startswith("Role: "):
        role_title = phase_name.removeprefix("Role: ")
        return f"{display_name} writing the role prompt for {role_title}"
    if phase_name.endswith(" Design Draft"):
        label = phase_name.removesuffix(" Design Draft")
        return f"{display_name} drafting the design for {label}"
    if " Feedback: " in phase_name:
        label, _, _role_title = phase_name.partition(" Feedback: ")
        return f"{display_name} reviewing the design for {label}"
    if phase_name.endswith(" Design Final"):
        label = phase_name.removesuffix(" Design Final")
        return f"{display_name} finalizing the design for {label}"
    if phase_name.endswith(" DevOps Proposal"):
        label = phase_name.removesuffix(" DevOps Proposal")
        return f"{display_name} preparing the DevOps proposal for {label}"
    if phase_name.endswith(" Implementation Plan"):
        label = phase_name.removesuffix(" Implementation Plan")
        return f"{display_name} planning the implementation for {label}"
    if phase_name.endswith(" Implementation Execute"):
        label = phase_name.removesuffix(" Implementation Execute")
        return f"{display_name} executing the implementation for {label}"
    if phase_name.endswith(" Implementation Review"):
        label = phase_name.removesuffix(" Implementation Review")
        return f"{display_name} reviewing the implementation for {label}"
    return f"{display_name} working on {phase_name}"


def _invoke_agent_with_status(
    phase_name: str,
    *,
    agent_name: str,
    attempt: int,
    invoke: Callable[[], str],
) -> str:
    """Run an agent call while showing concise progress for the current stage."""
    status_message = _agent_status_message(agent_name, phase_name)
    if attempt > 1:
        status_message = f"Retry {attempt}/{_MAX_RETRIES + 1}: {status_message}"

    if _supports_live_status():
        with _console.status(status_message, spinner="dots12", spinner_style="cyan"):
            return invoke()

    print(f"\n>> {status_message}...", flush=True)
    return invoke()


def _invoke_agent_with_progress(
    agent: Agent,
    context: dict[str, str],
    phase_name: str,
    *,
    feedback: str | None = None,
    attempt: int,
) -> str:
    """Run an agent while showing concise progress for the current stage."""
    return _invoke_agent_with_status(
        phase_name,
        agent_name=agent.name,
        attempt=attempt,
        invoke=lambda: agent.run(context, feedback=feedback),
    )


def _invoke_agent_plan_with_progress(
    agent: Agent,
    context: dict[str, str],
    phase_name: str,
    *,
    feedback: str | None = None,
    attempt: int,
) -> str:
    """Run an agent planning call while showing concise progress."""
    return _invoke_agent_with_status(
        phase_name,
        agent_name=agent.name,
        attempt=attempt,
        invoke=lambda: agent.plan(context, feedback=feedback),
    )


def _invoke_agent_execute_with_progress(
    agent: Agent,
    context: dict[str, str],
    phase_name: str,
    *,
    plan: str,
    feedback: str | None = None,
    attempt: int,
    auto_approve: bool = True,
) -> str:
    """Run an agent execution call while showing concise progress."""
    return _invoke_agent_with_status(
        phase_name,
        agent_name=agent.name,
        attempt=attempt,
        invoke=lambda: agent.execute(context, plan=plan, feedback=feedback, auto_approve=auto_approve),
    )


def _agent_loop(
    agent: Agent,
    context: dict[str, str],
    lint_fn: Callable[[str], list[str]],
    phase_name: str,
    *,
    founder_feedback: str | None = None,
) -> str:
    """Run an agent, retrying only transient backend failures."""
    feedback: str | None = founder_feedback

    for attempt in range(1, _MAX_RETRIES + 2):  # 1 initial + _MAX_RETRIES
        logger.debug("Agent loop: %s attempt %d/%d", agent.name, attempt, _MAX_RETRIES + 1)
        try:
            output = _invoke_agent_with_progress(agent, context, phase_name, feedback=feedback, attempt=attempt)
        except LLMInvocationError as exc:
            logger.warning(
                "Agent %s invocation failed (retryable=%s, reason=%s): %s",
                agent.name,
                exc.retryable,
                exc.reason,
                exc,
            )
            print(f"   Gemini call FAILED: {exc}")

            if exc.retryable and attempt <= _MAX_RETRIES:
                print(
                    "   Retrying because the Gemini failure was classified as transient"
                    f" ({exc.reason or 'transient error'})."
                )
                continue

            if exc.retryable:
                print(f"\nFATAL: {agent.name} hit a transient Gemini error after {_MAX_RETRIES + 1} attempts.")
            else:
                print(f"\nFATAL: {agent.name} hit a non-retryable Gemini error.")
                print("  → Not retrying automatically to avoid burning additional tokens.")
            sys.exit(1)
        except RuntimeError as exc:
            logger.warning("Agent %s invocation failed with unexpected runtime error: %s", agent.name, exc)
            print(f"\nFATAL: {agent.name} hit an unexpected non-retryable error: {exc}")
            print("  → Not retrying automatically to avoid burning additional tokens.")
            sys.exit(1)

        logger.debug(
            "Agent %s produced %d chars for %s (sha256=%s)",
            agent.name,
            len(output),
            phase_name,
            string_checksum_prefix(output),
        )
        print(f"   Response received: {_agent_status_message(agent.name, phase_name)}.")

        errors = lint_fn(output)
        if not errors:
            print(f"   Lint passed for {phase_name}.")
            return output

        print(f"   Lint FAILED ({len(errors)} error(s)):")
        for err in errors:
            print(f"     - {err}")

        company = _agent_company(agent)
        if company is not None:
            failed_path = write_failed_artifact(company, phase_name, output, errors, attempt=attempt)
            print(f"   Failed output saved for inspection: {failed_path}")

        logger.debug("Agent %s produced mechanically invalid output – failing without retry", agent.name)
        print(f"\nFATAL: {agent.name} produced output that failed mechanical validation.")
        print("  → Not retrying automatically to avoid burning additional tokens.")
        sys.exit(1)

    # Unreachable but keeps mypy happy.
    msg = "Unreachable"
    raise AssertionError(msg)


def _write_architecture(raw_arch: str, company: Path) -> None:
    """Parse CTO output and write architecture artifacts."""
    _, json_str, mermaid_str = _lint_architecture(raw_arch)

    arch_json_path = company / "artifacts" / "architecture.json"
    arch_json_path.write_text(json_str or "", encoding="utf-8")

    arch_md_path = company / "artifacts" / "architecture.md"
    arch_md_content = _render_architecture_markdown(json_str or "{}", mermaid_str or "")
    arch_md_path.write_text(arch_md_content, encoding="utf-8")

    print(f"\n✓ Architecture JSON written: {arch_json_path}")
    print(f"✓ Architecture diagram written: {arch_md_path}")


# ── Role generation (Phase C2) helpers ───────────────────────────────────

_ROLE_REQUIRED_SECTIONS = ["Output Format", "Strict Rules"]


def _lint_role(content: str) -> list[str]:
    """Validate a generated role system-prompt file."""
    errors: list[str] = []
    if not content or len(content) < 200:
        errors.append(f"Role file is too short ({len(content)} chars, minimum 200).")
        return errors

    if not re.match(r"^#\s+Role:", content):
        errors.append("Role file must start with a '# Role:' heading.")

    for section in _ROLE_REQUIRED_SECTIONS:
        pattern = rf"^##\s+{re.escape(section)}"
        if not re.search(pattern, content, re.MULTILINE):
            errors.append(f"Missing required section: '## {section}'")

    logger.debug("Role lint result: %d error(s)", len(errors))
    for err in errors:
        logger.debug("  Role lint error: %s", err)
    return errors


def _build_execution_plan_context(
    company: Path,
    vision_content: str,
    prd_content: str,
    architecture_json: str,
) -> dict[str, str]:
    """Build the base context for the execution-plan phase."""
    context = {
        "vision": vision_content,
        "prd": prd_content,
        "architecture": architecture_json,
    }
    template_path = company / "templates" / "execution_plan_template.md"
    if template_path.is_file():
        context["execution_plan_template"] = template_path.read_text(encoding="utf-8")
    return context


def _persist_execution_plan_artifact(raw_plan: str, company: Path) -> str:
    """Extract and persist execution-plan JSON, returning the JSON payload."""
    _, json_block = _lint_execution_plan(raw_plan)
    assert json_block is not None  # noqa: S101
    _write_execution_plan(json_block, company)
    return json_block


def _load_assigned_standards_content(standards_dir: Path, assigned_standards: list[str]) -> str:
    """Return the concatenated standards content for a role entry."""
    std_parts: list[str] = []
    for std_name in assigned_standards:
        std_path = standards_dir / std_name
        if std_path.is_file():
            std_parts.append(std_path.read_text(encoding="utf-8"))
    return "\n\n---\n\n".join(std_parts) if std_parts else "(no standards assigned)"


def _available_standard_paths(company: Path) -> list[Path]:
    """Return the current standards file paths in deterministic order."""
    standards_dir = company / "standards"
    if not standards_dir.is_dir():
        return []
    return sorted((path for path in standards_dir.iterdir() if path.is_file()), key=lambda path: path.name)


def _assigned_standard_paths(company: Path, roster_json: str) -> list[Path]:
    """Return the standards files referenced by the current approved roster."""
    standards_dir = company / "standards"
    try:
        roster = json.loads(roster_json)
    except json.JSONDecodeError:
        return []

    paths: list[Path] = []
    for entry in roster.get("hired_agents", []):
        if not isinstance(entry, dict):
            continue
        for filename in entry.get("assigned_standards", []):
            if isinstance(filename, str) and filename:
                paths.append(standards_dir / filename)

    unique_paths = {path.resolve(): path for path in paths}
    return sorted(unique_paths.values(), key=lambda path: path.name)


def _run_prd_phase(company: Path, vision_content: str, llm: LLMBackend) -> str:
    """Run the CPO PRD phase including founder review loop."""
    cpo = Agent(name="CPO", role_file=company / "roles" / "cpo.md", llm=llm)
    base_context = {"vision": vision_content}
    prd_content = _agent_loop(cpo, base_context, _lint_prd, "PRD")

    prd_path = company / "artifacts" / "prd.md"
    prd_path.write_text(prd_content, encoding="utf-8")
    print(f"\n✓ PRD written: {prd_path}")

    review = founder_review("PRD", prd_path, questions=_extract_founder_questions(prd_content))
    while True:
        if review.action == "approve":
            return prd_content

        if review.action == "answer_questions":
            logger.debug("Applying %d founder answer(s) locally to PRD", len(review.answers))
            prd_content = _apply_founder_answers_to_prd(prd_content, review.answers)
            prd_path.write_text(prd_content, encoding="utf-8")
            review = founder_review("PRD", prd_path, questions=_extract_founder_questions(prd_content))
            continue

        rerun_context = base_context
        if review.action in {"modify", "request_more_questions"}:
            rerun_context = _build_revision_context(base_context, "current_prd", prd_content)

        prd_content = _agent_loop(
            cpo,
            rerun_context,
            _lint_prd,
            "PRD",
            founder_feedback=_review_feedback(review),
        )
        prd_path.write_text(prd_content, encoding="utf-8")
        review = founder_review("PRD", prd_path, questions=_extract_founder_questions(prd_content))


def _run_architecture_phase(company: Path, vision_content: str, prd_content: str, llm: LLMBackend) -> str:
    """Run the CTO Architecture phase including founder review loop.

    Returns the architecture JSON string for downstream phases.
    """
    cto = Agent(name="CTO", role_file=company / "roles" / "cto.md", llm=llm)
    arch_context = {"vision": vision_content, "prd": prd_content}

    raw_arch = _agent_loop(
        cto,
        arch_context,
        lambda c: _lint_architecture(c)[0],
        "Architecture",
    )
    _write_architecture(raw_arch, company)

    arch_md_path = company / "artifacts" / "architecture.md"
    review = founder_review("Architecture", arch_md_path, questions=_extract_founder_questions(raw_arch))
    while True:
        if review.action == "approve":
            break

        if review.action == "answer_questions":
            logger.debug("Applying %d founder answer(s) locally to architecture", len(review.answers))
            raw_arch = _apply_founder_answers_to_content(raw_arch, review.answers)
            _write_architecture(raw_arch, company)
            review = founder_review("Architecture", arch_md_path, questions=_extract_founder_questions(raw_arch))
            continue

        rerun_context = arch_context
        if review.action in {"modify", "request_more_questions"}:
            rerun_context = _build_revision_context(arch_context, "current_architecture", raw_arch)

        previous_questions = _extract_founder_question_items(raw_arch)

        raw_arch = _agent_loop(
            cto,
            rerun_context,
            lambda c: _lint_architecture(c)[0],
            "Architecture",
            founder_feedback=_review_feedback(review),
        )

        if review.action == "request_more_questions" and not _has_new_unanswered_questions(
            previous_questions, raw_arch
        ):
            raw_arch = _agent_loop(
                cto,
                rerun_context,
                lambda c: _lint_architecture(c)[0],
                "Architecture",
                founder_feedback=(
                    (_review_feedback(review) or _REQUEST_MORE_QUESTIONS_FEEDBACK)
                    + "\n\n"
                    + _REQUEST_MORE_QUESTIONS_ESCALATION
                ),
            )
            if not _has_new_unanswered_questions(previous_questions, raw_arch):
                print("\nFATAL: CTO did not produce any new founder questions after the requested follow-up round.")
                print("  → Stopping to avoid silently returning the same architecture review flow.")
                sys.exit(1)

        _write_architecture(raw_arch, company)
        review = founder_review("Architecture", arch_md_path, questions=_extract_founder_questions(raw_arch))

    # Return the architecture JSON for downstream phases.
    arch_json_path = company / "artifacts" / "architecture.json"
    return arch_json_path.read_text(encoding="utf-8")


def _run_execution_plan_phase(
    company: Path,
    vision_content: str,
    prd_content: str,
    architecture_json: str,
    llm: LLMBackend,
) -> str:
    """Run the VP Engineering execution-plan phase including founder review."""
    vpe = Agent(name="VP Engineering", role_file=company / "roles" / "vpe.md", llm=llm)
    base_context = _build_execution_plan_context(company, vision_content, prd_content, architecture_json)

    raw_plan = _agent_loop(
        vpe,
        base_context,
        lambda c: _lint_execution_plan(c)[0],
        "Execution Plan",
    )
    json_block = _persist_execution_plan_artifact(raw_plan, company)

    plan_md_path = company / "artifacts" / "execution_plan.md"
    review = founder_review("Execution Plan", plan_md_path, questions=_extract_founder_questions(raw_plan))
    while True:
        if review.action == "approve":
            return json_block

        if review.action == "answer_questions":
            logger.debug("Applying %d founder answer(s) locally to execution plan", len(review.answers))
            raw_plan = _apply_founder_answers_to_content(raw_plan, review.answers)
            json_block = _persist_execution_plan_artifact(raw_plan, company)
            review = founder_review("Execution Plan", plan_md_path, questions=_extract_founder_questions(raw_plan))
            continue

        if review.action == "modify" and review.feedback and review.feedback.strip().startswith("{"):
            edit_errors, _ = _lint_execution_plan(f"```json\n{review.feedback}\n```")
            if edit_errors:
                print("\n  Edited execution plan has validation errors:")
                for err in edit_errors:
                    print(f"    - {err}")
                print("  Please try again.\n")
                review = founder_review("Execution Plan", plan_md_path, questions=_extract_founder_questions(raw_plan))
                continue
            assert review.feedback is not None  # noqa: S101
            json_block = review.feedback
            raw_plan = f"```json\n{json_block}\n```"
            _write_execution_plan(json_block, company)
            review = founder_review("Execution Plan", plan_md_path, questions=_extract_founder_questions(raw_plan))
            continue

        rerun_context = base_context
        if review.action in {"modify", "request_more_questions"}:
            rerun_context = _build_revision_context(base_context, "current_execution_plan", raw_plan)

        raw_plan = _agent_loop(
            vpe,
            rerun_context,
            lambda c: _lint_execution_plan(c)[0],
            "Execution Plan",
            founder_feedback=_review_feedback(review),
        )
        json_block = _persist_execution_plan_artifact(raw_plan, company)
        review = founder_review("Execution Plan", plan_md_path, questions=_extract_founder_questions(raw_plan))


def _run_roster_phase(company: Path, architecture_json: str, execution_plan_json: str, llm: LLMBackend) -> str:
    """Run the Hiring Manager role-elaboration phase.

    Returns the elaborated roster JSON string.
    """
    hm = Agent(
        name="Hiring Manager",
        role_file=company / "roles" / "hiring_manager.md",
        llm=llm,
    )
    standards_dir = company / "standards"
    execution_plan = json.loads(execution_plan_json)

    # Build list of available standards filenames.
    available = sorted(f.name for f in standards_dir.iterdir() if f.is_file()) if standards_dir.is_dir() else []

    context = {
        "architecture": architecture_json,
        "execution_plan": execution_plan_json,
        "selected_team": json.dumps(execution_plan.get("selected_team", []), indent=2),
        "available_standards": "\n".join(f"- {s}" for s in available) if available else "(none)",
    }

    raw_roster = _agent_loop(
        hm,
        context,
        lambda c: _lint_roster(c, standards_dir=standards_dir),
        "Roster",
    )

    json_block = _extract_json_block(raw_roster)
    assert json_block is not None  # lint passed, so this is guaranteed  # noqa: S101
    _write_roster(json_block, company)
    return json_block


def _generate_single_role(
    entry: dict,
    company: Path,
    shared_context: dict[str, str],
    llm: LLMBackend,
) -> None:
    """Generate a single role file from a roster entry."""
    title = entry["title"]
    role_path = company / "roles" / entry["filename"]
    if entry["filename"] in MANDATORY_CORE_ROLE_FILENAMES and role_path.is_file():
        print(f"   ✓ Using bundled core role: {title} → {role_path}")
        return

    standards_dir = company / "standards"
    standards_content = _load_assigned_standards_content(standards_dir, entry.get("assigned_standards", []))

    context = {
        **shared_context,
        "role_title": title,
        "role_responsibility": entry["responsibility"],
        "role_brief": json.dumps(entry, indent=2),
        "assigned_standards": standards_content,
    }

    rw = Agent(name="Role Writer", role_file=company / "roles" / "role_writer.md", llm=llm)
    role_content = _agent_loop(rw, context, _lint_role, f"Role: {title}")

    role_path.write_text(role_content, encoding="utf-8")
    print(f"   ✓ Generated role: {title} → {role_path}")


def _run_role_generation(
    company: Path,
    architecture_json: str,
    execution_plan_json: str,
    roster_json: str,
    llm: LLMBackend,
) -> None:
    """Generate individual role files for each entry in the approved roster."""
    roster = json.loads(roster_json)
    agents_list = roster["hired_agents"]

    # Load the role template.
    template_path = company / "templates" / "role_template.md"
    role_template = template_path.read_text(encoding="utf-8") if template_path.is_file() else ""
    if template_path.is_file():
        logger.debug(
            "Loaded role template %s (%d chars, sha256=%s)",
            template_path,
            len(role_template),
            string_checksum_prefix(role_template),
        )
    else:
        logger.debug("Role template missing at %s; continuing with empty template", template_path)

    total = len(agents_list)
    shared_context = {
        "architecture": architecture_json,
        "execution_plan": execution_plan_json,
        "role_template": role_template,
    }

    print(f"\n>> Generating {total} role file(s)…")

    for idx, entry in enumerate(agents_list, 1):
        print(f"\n── Role {idx}/{total}: {entry['title']} ──")
        _generate_single_role(entry, company, shared_context, llm)

    print(f"\n✓ Generated {total} role file(s)")


def _phase_loop_state_name(phase_id: str, step: str) -> str:
    """Return the pipeline-state key for a phase-loop *step*."""
    return f"phase-loop:{phase_id}:{step}"


def _implementation_turn_state_name(phase_id: str, turn_index: int, step: str) -> str:
    """Return the pipeline-state key for one implementation turn step."""
    return _phase_loop_state_name(phase_id, f"turn:{turn_index}:{step}")


def _empty_phase_status(phase: str) -> PhaseStatus:
    """Return an empty phase status for a missing implementation-turn record."""
    return PhaseStatus(
        phase=phase,
        completed_at=None,
        recorded_inputs={},
        recorded_outputs={},
        current_inputs={},
        current_outputs={},
        changed_inputs=[],
        changed_outputs=[],
        missing_outputs=[],
    )


def _phase_label(phase_data: dict, phase_index: int) -> str:
    """Return a human-readable label for an execution-plan phase."""
    phase_id = phase_data.get("id", f"phase_{phase_index + 1}")
    phase_name = phase_data.get("name", f"Phase {phase_index + 1}")
    return f"{phase_id} - {phase_name}"


def _write_text_artifact(path: Path, content: str) -> None:
    """Write *content* to *path*, creating parent directories when needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _phase_team_entries(roster_json: str, phase_data: dict) -> list[dict]:
    """Return the roster entries assigned to *phase_data*."""
    roster = json.loads(roster_json)
    hired_agents = roster.get("hired_agents", [])
    by_title = {
        entry["title"]: entry
        for entry in hired_agents
        if isinstance(entry, dict) and isinstance(entry.get("title"), str)
    }

    entries: list[dict] = []
    for title in phase_data.get("selected_team_roles", []):
        entry = by_title.get(title)
        if entry is None:
            msg = f"Phase references role '{title}' but roster.json does not contain that role."
            raise RuntimeError(msg)
        entries.append(entry)
    return entries


def _phase_role_paths(company: Path, team_entries: list[dict]) -> list[Path]:
    """Return the current role prompt paths for *team_entries*."""
    paths: list[Path] = []
    for entry in team_entries:
        filename = entry.get("filename")
        if isinstance(filename, str) and filename:
            paths.append(company / "roles" / filename)
    return paths


def _phase_design_request(phase_data: dict, team_entries: list[dict], *, harmonized: bool) -> str:
    """Return the prompt instructions for a phase design artifact."""
    phase_name = phase_data.get("name", phase_data.get("id", "Current Phase"))
    role_titles = ", ".join(entry["title"] for entry in team_entries)
    mode = "Produce the harmonized final phase design." if harmonized else "Produce the initial phase design draft."
    return (
        f"{mode}\n"
        f"The current phase is '{phase_name}'. Return Markdown only using this exact structure:\n\n"
        f"# Phase Design: {phase_name}\n\n"
        "## Phase Summary\n"
        "- Summarize the approved scope, the delivery objective, and the handoff boundary for this phase.\n\n"
        "## Task Mapping\n"
        "```json\n"
        "{\n"
        '  "tasks": [\n'
        "    {\n"
        '      "id": "task_one",\n'
        '      "title": "Task title",\n'
        f'      "owner": "One of: {role_titles}",\n'
        '      "objective": "Why this task exists in this phase",\n'
        '      "depends_on": ["optional_previous_task_id"],\n'
        '      "deliverables": ["Concrete output"],\n'
        '      "acceptance_criteria": ["How the team will know this task is done"]\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "```\n\n"
        "The depends_on array is optional for each task object and defaults to [] when omitted. "
        "Use [] when a task has no prerequisites.\n\n"
        "## Required Tooling\n"
        "- List every tool, package, or environment prerequisite needed for this phase. "
        "Use '- None.' if no tooling changes are required.\n\n"
        "## Sequencing Notes\n"
        "- Explain ordering, dependencies, and handoff expectations.\n\n"
        "Use the current validation contract context when planning this phase. When a task changes product behavior, "
        "capture the required validation coverage or an explicit known-gap update in the task deliverables or "
        "acceptance criteria.\n\n"
        "Keep task ids stable, task ownership explicit, and dependency order explicit. Do not assign work outside "
        "the approved phase team."
    )


def _phase_design_output_paths(paths: PhaseArtifactPaths, team_entries: list[dict]) -> list[Path]:
    """Return tracked outputs for a phase design step."""
    return [
        paths.draft_path,
        paths.final_path,
        *[paths.feedback_path(entry["title"]) for entry in team_entries],
        paths.task_mapping_json_path,
        paths.task_mapping_md_path,
    ]


def _persist_phase_task_mapping_from_design(
    phase_design_content: str,
    *,
    allowed_roles: set[str],
    paths: PhaseArtifactPaths,
    phase_label: str,
) -> dict:
    """Extract, validate, and persist the canonical task mapping from a final phase design."""
    json_block = extract_fenced_code_block(phase_design_content, "json")
    if json_block is None:
        raise ValueError("No fenced ```json``` task-mapping block found in final phase design output.")

    errors, task_mapping = lint_phase_task_mapping_json(json_block, allowed_roles=allowed_roles)
    if errors or task_mapping is None:
        raise ValueError(f"Invalid phase task mapping: {'; '.join(errors)}")

    write_phase_task_mapping(task_mapping, paths, phase_label=phase_label)
    print(f"✓ Phase task mapping written: {paths.task_mapping_json_path}")
    print(f"✓ Phase task mapping summary written: {paths.task_mapping_md_path}")
    return task_mapping


def _sync_saved_phase_design_artifacts(  # pylint: disable=too-many-arguments,too-many-locals
    exec_ctx: PipelineExecutionContext,
    *,
    status: PhaseStatus,
    phase_key: str,
    input_paths: list[Path],
    output_paths: list[Path],
    paths: PhaseArtifactPaths,
    allowed_roles: set[str],
    phase_label: str,
) -> PhaseStatus:
    """Backfill derived phase-design artifacts and refresh state for slice-3 migration cases."""
    if not status.has_record or not paths.final_path.is_file():
        return status

    validation_contract_json_path, _ = validation_contract_paths(exec_ctx.company)
    validation_input_keys = set(snapshot_paths(exec_ctx.workdir, [validation_contract_json_path]))
    task_mapping_output_keys = set(
        snapshot_paths(exec_ctx.workdir, [paths.task_mapping_json_path, paths.task_mapping_md_path])
    )
    changed_input_keys = set(status.changed_inputs)
    output_drift = set(status.changed_outputs) | set(status.missing_outputs)

    input_migration_only = not changed_input_keys or (
        changed_input_keys.issubset(validation_input_keys)
        and all(key not in status.recorded_inputs for key in changed_input_keys)
    )
    output_migration_only = not output_drift or output_drift.issubset(task_mapping_output_keys)
    if not input_migration_only or not output_migration_only or not (changed_input_keys or output_drift):
        return status

    if output_drift:
        final_design = paths.final_path.read_text(encoding="utf-8")
        _persist_phase_task_mapping_from_design(
            final_design,
            allowed_roles=allowed_roles,
            paths=paths,
            phase_label=phase_label,
        )

    mark_phase_complete(exec_ctx.workdir, exec_ctx.state, phase_key, input_paths=input_paths, output_paths=output_paths)
    return _evaluate_phase_status(exec_ctx, phase_key, input_paths=input_paths, output_paths=output_paths)


def _phase_feedback_request(role_title: str) -> str:
    """Return the prompt instructions for role-specific phase feedback."""
    return (
        f"Review the current phase design draft from the perspective of the '{role_title}' role. "
        "Return Markdown only using the exact structure required by your role prompt. "
        "Use '- None.' when a section has no material feedback."
    )


def _devops_proposal_request(phase_data: dict, script_path: Path) -> str:
    """Return the prompt instructions for a DevOps setup proposal."""
    phase_name = phase_data.get("name", phase_data.get("id", "Current Phase"))
    return (
        f"Produce a guarded DevOps setup proposal for '{phase_name}'. "
        "Return Markdown only using this exact structure:\n\n"
        f"# DevOps Setup Proposal: {phase_name}\n\n"
        "## Execution Summary\n"
        "Describe what the script will do and why those steps are necessary for this phase.\n\n"
        "## Safety Notes\n"
        "- List the operational safeguards, idempotency protections, and assumptions.\n"
        "- Include '- None.' only if there are genuinely no extra safety notes.\n\n"
        "## Repo Impact\n"
        "- State what will change.\n"
        "- State what will remain untouched.\n"
        f"- The generated script will be written to {script_path}.\n\n"
        "## Setup Script\n"
        "```bash\n"
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "trap 'echo \"DevOps phase setup failed at line $LINENO while running: $BASH_COMMAND\" >&2' ERR\n"
        "```\n\n"
        "The script must stay non-interactive, idempotent where practical, must not invoke git, "
        "and must not overwrite tracked repository files or existing DevContainer bootstrap files."
    )


def _run_phase_design_step(  # pylint: disable=too-many-arguments,too-many-locals
    company: Path,
    *,
    vision_content: str,
    prd_content: str,
    architecture_json: str,
    execution_plan_json: str,
    roster_json: str,
    phase_data: dict,
    phase_index: int,
    paths: PhaseArtifactPaths,
    llm: LLMBackend,
) -> str:
    """Generate the draft, feedback, and final design artifacts for one phase."""
    team_entries = _phase_team_entries(roster_json, phase_data)
    allowed_roles = {entry["title"] for entry in team_entries}
    phase_json = json.dumps(phase_data, indent=2)
    team_briefs = json.dumps(team_entries, indent=2)
    label = _phase_label(phase_data, phase_index)
    validation_contract = load_validation_contract(company) or ensure_validation_contract(company)
    validation_contract_json = json.dumps(validation_contract, indent=2)
    validation_contract_markdown = render_validation_contract_markdown(validation_contract)

    development_lead = Agent(
        name="Development Lead",
        role_file=company / "roles" / "development_lead.md",
        llm=llm,
    )
    draft_context = {
        "phase_design_request": _phase_design_request(phase_data, team_entries, harmonized=False),
        "vision": vision_content,
        "prd": prd_content,
        "architecture": architecture_json,
        "execution_plan": execution_plan_json,
        "current_phase": phase_json,
        "phase_team_briefs": team_briefs,
        "validation_contract_json": validation_contract_json,
        "validation_contract_markdown": validation_contract_markdown,
    }
    draft = _agent_loop(
        development_lead,
        draft_context,
        lambda content: lint_phase_design(content, allowed_roles=allowed_roles)[0],
        f"{label} Design Draft",
    )
    _write_text_artifact(paths.draft_path, draft)
    print(f"\n✓ Phase design draft written: {paths.draft_path}")

    feedback_role_file = company / "roles" / "phase_feedback_reviewer.md"
    feedback_blocks: list[str] = []
    for entry in team_entries:
        title = entry["title"]
        role_path = company / "roles" / entry["filename"]
        reviewer = Agent(name=f"{title} Feedback", role_file=feedback_role_file, llm=llm)
        feedback = _agent_loop(
            reviewer,
            {
                "phase_feedback_request": _phase_feedback_request(title),
                "role_title": title,
                "role_brief": json.dumps(entry, indent=2),
                "role_prompt": role_path.read_text(encoding="utf-8"),
                "current_phase": phase_json,
                "phase_design_draft": draft,
            },
            lint_phase_feedback,
            f"{label} Feedback: {title}",
        )
        feedback_path = paths.feedback_path(title)
        _write_text_artifact(feedback_path, feedback)
        print(f"✓ Phase feedback written: {feedback_path}")
        feedback_blocks.append(feedback)

    final = _agent_loop(
        development_lead,
        {
            **draft_context,
            "phase_design_request": _phase_design_request(phase_data, team_entries, harmonized=True),
            "phase_design_draft": draft,
            "phase_feedback": "\n\n---\n\n".join(feedback_blocks),
        },
        lambda content: lint_phase_design(content, allowed_roles=allowed_roles)[0],
        f"{label} Design Final",
    )
    _write_text_artifact(paths.final_path, final)
    print(f"✓ Phase design final written: {paths.final_path}")
    _persist_phase_task_mapping_from_design(final, allowed_roles=allowed_roles, paths=paths, phase_label=label)
    return final


def _phase_design_input_paths(exec_ctx: PipelineExecutionContext, team_entries: list[dict]) -> list[Path]:
    """Return tracked inputs for a phase design step."""
    validation_contract_json_path, _ = validation_contract_paths(exec_ctx.company)
    return [
        exec_ctx.vision_path,
        exec_ctx.company / "artifacts" / "prd.md",
        exec_ctx.company / "artifacts" / "architecture.json",
        exec_ctx.company / "artifacts" / "execution_plan.json",
        exec_ctx.company / "artifacts" / "roster.json",
        validation_contract_json_path,
        exec_ctx.company / "roles" / "development_lead.md",
        exec_ctx.company / "roles" / "phase_feedback_reviewer.md",
        *_phase_role_paths(exec_ctx.company, team_entries),
    ]


def _turn_label(phase_label: str, turn: PhaseImplementationTurn) -> str:
    """Return a human-readable label for one implementation turn."""
    return f"{phase_label} Turn {turn.turn_index}"


def _standard_paths_for_entry(company: Path, entry: dict) -> list[Path]:
    """Return standard files assigned to a roster entry."""
    standards_dir = company / "standards"
    paths: list[Path] = []
    for filename in entry.get("assigned_standards", []):
        if isinstance(filename, str) and filename:
            paths.append(standards_dir / filename)
    return paths


def _implementation_turn_base_input_paths(
    exec_ctx: PipelineExecutionContext,
    paths: PhaseArtifactPaths,
    turn: PhaseImplementationTurn,
) -> list[Path]:
    """Return the tracked base inputs for one implementation turn."""
    role_filename = turn.roster_entry.get("filename")
    if not isinstance(role_filename, str) or not role_filename:
        raise RuntimeError(f"Roster entry for {turn.owner_title!r} is missing a role filename.")

    validation_contract_json_path, _ = validation_contract_paths(exec_ctx.company)
    return [
        paths.final_path,
        paths.task_mapping_json_path,
        exec_ctx.company / "roles" / role_filename,
        *_standard_paths_for_entry(exec_ctx.company, turn.roster_entry),
        validation_contract_json_path,
    ]


def _normalized_string_list(value: object) -> list[str]:
    """Return a normalized list of strings from *value*."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _metadata_string_list(metadata: dict[str, object], key: str) -> list[str] | None:
    """Return a string list from metadata, or ``None`` when absent or invalid."""
    value = metadata.get(key)
    if not isinstance(value, list):
        return None
    return [item for item in value if isinstance(item, str)]


def _resolve_repo_relative_path(workdir: Path, repo_relative_path: str) -> Path:
    """Return an absolute path for a repo-relative worktree path."""
    root = repo_root(workdir) if is_git_repo(workdir) else workdir
    return root / repo_relative_path


def _implementation_commit_output_paths(
    exec_ctx: PipelineExecutionContext,
    paths: PhaseArtifactPaths,
    turn: PhaseImplementationTurn,
    attempt: int,
    approved_paths: list[str],
) -> list[Path]:
    """Return the tracked outputs for one implementation-turn commit step."""
    commit_summary_path = paths.implementation_commit_path(turn.turn_index, turn.owner_title, attempt)
    changed_paths = [_resolve_repo_relative_path(exec_ctx.workdir, path) for path in approved_paths]
    return [commit_summary_path, *changed_paths]


def _implementation_turn_step_paths(
    exec_ctx: PipelineExecutionContext,
    paths: PhaseArtifactPaths,
    turn: PhaseImplementationTurn,
    *,
    step: str,
    attempt: int,
    approved_paths: list[str] | None = None,
) -> tuple[list[Path], list[Path]]:
    """Return tracked inputs and outputs for one implementation-turn step."""
    base_inputs = _implementation_turn_base_input_paths(exec_ctx, paths, turn)
    plan_path = paths.implementation_plan_path(turn.turn_index, turn.owner_title, attempt)
    execute_path = paths.implementation_execution_path(turn.turn_index, turn.owner_title, attempt)
    validation_path = paths.implementation_validation_path(turn.turn_index, turn.owner_title, attempt)
    scope_path = paths.implementation_scope_path(turn.turn_index, turn.owner_title, attempt)
    review_path = paths.implementation_review_path(turn.turn_index, turn.owner_title, attempt)

    if step == "plan":
        return base_inputs, [plan_path]
    if step == "execute":
        return [*base_inputs, plan_path], [execute_path]
    if step == "validate":
        return [*base_inputs, execute_path], [validation_path, scope_path]
    if step == "review":
        return [
            *base_inputs,
            exec_ctx.company / "roles" / "development_lead.md",
            plan_path,
            execute_path,
            validation_path,
            scope_path,
        ], [review_path]
    if step == "commit":
        return [*base_inputs, review_path, validation_path, scope_path], _implementation_commit_output_paths(
            exec_ctx,
            paths,
            turn,
            attempt,
            approved_paths or [],
        )

    raise ValueError(f"Unsupported implementation step: {step}")


def _render_implementation_commit_artifact(
    phase_label: str,
    turn: PhaseImplementationTurn,
    *,
    approved_paths: list[str],
    commit_hash: str,
) -> str:
    """Render a Markdown commit summary for one approved implementation turn."""
    lines = [
        f"# Commit Summary: {_turn_label(phase_label, turn)}",
        "",
        f"- **Owner:** {turn.owner_title}",
        f"- **Task IDs:** {', '.join(turn.task_ids)}",
        f"- **Commit Hash:** {commit_hash or '(no commit created)'}",
        "",
        "## Approved Paths",
    ]
    if approved_paths:
        lines.extend(f"- {path}" for path in approved_paths)
    else:
        lines.append("- None.")
    lines.append("")
    return "\n".join(lines)


def _evaluate_implementation_commit_status(
    exec_ctx: PipelineExecutionContext,
    *,
    phase_id: str,
    paths: PhaseArtifactPaths,
    turn: PhaseImplementationTurn,
) -> PhaseStatus:
    """Return the persisted commit-step status for one implementation turn."""
    phase = _implementation_turn_state_name(phase_id, turn.turn_index, "commit")
    record = _phase_record(exec_ctx.state, phase)
    metadata = record.get("metadata", {}) if isinstance(record.get("metadata"), dict) else {}
    attempt = metadata.get("attempt")
    approved_paths = metadata.get("approved_paths")
    if not isinstance(attempt, int) or attempt < 1:
        return _empty_phase_status(phase)

    approved_path_list = (
        [path for path in approved_paths if isinstance(path, str)] if isinstance(approved_paths, list) else []
    )
    input_paths, output_paths = _implementation_turn_step_paths(
        exec_ctx,
        paths,
        turn,
        step="commit",
        attempt=attempt,
        approved_paths=approved_path_list,
    )
    return _evaluate_phase_status(
        exec_ctx,
        phase,
        input_paths=input_paths,
        output_paths=output_paths,
    )


def _evaluate_implementation_turn_step_status(
    exec_ctx: PipelineExecutionContext,
    *,
    phase_id: str,
    paths: PhaseArtifactPaths,
    turn: PhaseImplementationTurn,
    step: str,
) -> ImplementationTurnStepStatus:
    """Return the persisted status for one implementation-turn step."""
    phase = _implementation_turn_state_name(phase_id, turn.turn_index, step)
    record = _phase_record(exec_ctx.state, phase)
    metadata = record.get("metadata", {}) if isinstance(record.get("metadata"), dict) else {}
    attempt = metadata.get("attempt")
    if not isinstance(attempt, int) or attempt < 1:
        return ImplementationTurnStepStatus(step=step, phase_status=_empty_phase_status(phase), metadata=metadata)

    approved_paths = _metadata_string_list(metadata, "approved_paths") if step == "commit" else None
    input_paths, output_paths = _implementation_turn_step_paths(
        exec_ctx,
        paths,
        turn,
        step=step,
        attempt=attempt,
        approved_paths=approved_paths,
    )
    return ImplementationTurnStepStatus(
        step=step,
        phase_status=_evaluate_phase_status(exec_ctx, phase, input_paths=input_paths, output_paths=output_paths),
        metadata=metadata,
    )


def _record_implementation_turn_step(
    exec_ctx: PipelineExecutionContext,
    *,
    phase_id: str,
    paths: PhaseArtifactPaths,
    turn: PhaseImplementationTurn,
    step: str,
    attempt: int,
    metadata: dict[str, object],
) -> None:
    """Persist a completion marker for one implementation-turn step."""
    approved_paths = _metadata_string_list(metadata, "approved_paths") if step == "commit" else None
    input_paths, output_paths = _implementation_turn_step_paths(
        exec_ctx,
        paths,
        turn,
        step=step,
        attempt=attempt,
        approved_paths=approved_paths,
    )
    mark_phase_complete(
        exec_ctx.workdir,
        exec_ctx.state,
        _implementation_turn_state_name(phase_id, turn.turn_index, step),
        input_paths=input_paths,
        output_paths=output_paths,
        metadata={
            "owner_title": turn.owner_title,
            "task_ids": turn.task_ids,
            "attempt": attempt,
            **metadata,
        },
    )


def _review_from_step_metadata(metadata: dict[str, object]) -> dict[str, object]:
    """Rebuild a normalized review payload from persisted review metadata."""
    return {
        "decision": metadata.get("decision", "revise"),
        "summary": metadata.get("summary", "Revise the turn based on Development Lead feedback."),
        "scope_findings": _normalized_string_list(metadata.get("scope_findings")),
        "standards_findings": _normalized_string_list(metadata.get("standards_findings")),
        "validation_findings": _normalized_string_list(metadata.get("validation_findings")),
        "required_follow_up": _normalized_string_list(metadata.get("required_follow_up")),
    }


def _implementation_retry_feedback_from_saved_state(
    paths: PhaseArtifactPaths,
    turn: PhaseImplementationTurn,
    review_metadata: dict[str, object],
    *,
    validation_passed: bool,
) -> str:
    """Return retry feedback reconstructed from persisted turn artifacts."""
    validation_report = None
    attempt = review_metadata.get("attempt")
    if isinstance(attempt, int) and attempt > 0 and not validation_passed:
        validation_report = paths.implementation_validation_path(turn.turn_index, turn.owner_title, attempt).read_text(
            encoding="utf-8"
        )
    return _implementation_retry_feedback(
        _review_from_step_metadata(review_metadata), validation_report=validation_report
    )


def _validate_implementation_commit_scope(
    exec_ctx: PipelineExecutionContext,
    *,
    baseline_changed_paths: list[str],
    approved_paths: list[str],
) -> int | None:
    """Refuse a turn commit when unapproved paths appeared after review."""
    if exec_ctx.options.stage_all:
        return None

    baseline_set = set(baseline_changed_paths)
    current_turn_paths = [path for path in _implementation_changed_paths(exec_ctx) if path not in baseline_set]
    extra_paths = sorted(set(current_turn_paths) - set(approved_paths))
    if extra_paths:
        print(
            f"\nError: unapproved changed paths appeared before commit: {_format_paths(extra_paths)}",
            file=sys.stderr,
        )
        return 1
    return None


def _classify_implementation_turn_resume(
    exec_ctx: PipelineExecutionContext,
    *,
    phase_id: str,
    paths: PhaseArtifactPaths,
    turn: PhaseImplementationTurn,
    force_rerun_from_plan: bool = False,
) -> tuple[ImplementationTurnResumePlan, dict[str, ImplementationTurnStepStatus]]:
    """Return how one implementation turn should resume from persisted state."""
    statuses = {
        step: _evaluate_implementation_turn_step_status(
            exec_ctx,
            phase_id=phase_id,
            paths=paths,
            turn=turn,
            step=step,
        )
        for step in ("plan", "execute", "validate", "review", "commit")
    }
    latest_attempt = max((status.attempt or 0) for status in statuses.values())
    next_attempt = max(1, latest_attempt + 1)

    if force_rerun_from_plan:
        return ImplementationTurnResumePlan(action="rerun", attempt=next_attempt, start_step="plan"), statuses

    if all(statuses[step].is_current for step in ("plan", "execute", "validate", "review")):
        review_metadata = statuses["review"].metadata
        validation_passed = statuses["validate"].metadata.get("passed") is True
        if review_metadata.get("decision") == "approve" and validation_passed and not exec_ctx.options.no_commit:
            return (
                ImplementationTurnResumePlan(
                    action="commit",
                    attempt=statuses["review"].attempt or max(1, latest_attempt),
                    start_step="commit",
                    approved_paths=_metadata_string_list(review_metadata, "approved_paths") or [],
                    baseline_changed_paths=_metadata_string_list(review_metadata, "baseline_changed_paths") or [],
                ),
                statuses,
            )

        if review_metadata.get("decision") == "revise" or not validation_passed:
            return (
                ImplementationTurnResumePlan(
                    action="rerun",
                    attempt=next_attempt,
                    start_step="plan",
                    feedback=_implementation_retry_feedback_from_saved_state(
                        paths,
                        turn,
                        review_metadata,
                        validation_passed=validation_passed,
                    ),
                ),
                statuses,
            )

    plan_status = statuses["plan"]
    if not plan_status.has_record:
        return ImplementationTurnResumePlan(action="resume", attempt=1, start_step="plan"), statuses
    if not plan_status.is_current:
        return ImplementationTurnResumePlan(action="rerun", attempt=next_attempt, start_step="plan"), statuses

    plan_baseline = _metadata_string_list(plan_status.metadata, "baseline_changed_paths")
    if plan_baseline is None:
        return ImplementationTurnResumePlan(action="rerun", attempt=next_attempt, start_step="plan"), statuses

    execute_status = statuses["execute"]
    if not execute_status.has_record or execute_status.phase_status.missing_outputs:
        return (
            ImplementationTurnResumePlan(
                action="resume",
                attempt=plan_status.attempt or 1,
                start_step="execute",
                baseline_changed_paths=plan_baseline,
            ),
            statuses,
        )
    if not execute_status.is_current:
        return ImplementationTurnResumePlan(action="rerun", attempt=next_attempt, start_step="plan"), statuses

    execute_baseline = _metadata_string_list(execute_status.metadata, "baseline_changed_paths")
    if execute_baseline is None:
        return ImplementationTurnResumePlan(action="rerun", attempt=next_attempt, start_step="plan"), statuses

    validate_status = statuses["validate"]
    if not validate_status.has_record or validate_status.phase_status.missing_outputs:
        return (
            ImplementationTurnResumePlan(
                action="resume",
                attempt=execute_status.attempt or plan_status.attempt or 1,
                start_step="validate",
                baseline_changed_paths=execute_baseline,
            ),
            statuses,
        )
    if not validate_status.is_current:
        return ImplementationTurnResumePlan(action="rerun", attempt=next_attempt, start_step="plan"), statuses

    validate_baseline = _metadata_string_list(validate_status.metadata, "baseline_changed_paths")
    if validate_baseline is None:
        return ImplementationTurnResumePlan(action="rerun", attempt=next_attempt, start_step="plan"), statuses

    review_status = statuses["review"]
    if not review_status.has_record or review_status.phase_status.missing_outputs:
        return (
            ImplementationTurnResumePlan(
                action="resume",
                attempt=validate_status.attempt or execute_status.attempt or plan_status.attempt or 1,
                start_step="review",
                baseline_changed_paths=validate_baseline,
            ),
            statuses,
        )
    if not review_status.is_current:
        return ImplementationTurnResumePlan(action="rerun", attempt=next_attempt, start_step="plan"), statuses

    return ImplementationTurnResumePlan(action="rerun", attempt=next_attempt, start_step="plan"), statuses


def _commit_implementation_turn(
    exec_ctx: PipelineExecutionContext,
    *,
    phase_id: str,
    phase_data: dict,
    phase_label: str,
    paths: PhaseArtifactPaths,
    turn: PhaseImplementationTurn,
    attempt: int,
    approved_paths: list[str],
    baseline_changed_paths: list[str],
) -> int | None:
    """Commit an approved implementation turn and persist durable commit evidence."""
    approved_path_list = sorted(dict.fromkeys(approved_paths))
    err = _validate_implementation_commit_scope(
        exec_ctx,
        baseline_changed_paths=baseline_changed_paths,
        approved_paths=approved_path_list,
    )
    if err is not None:
        return err

    commit_name = f"{phase_data.get('id', f'phase_{turn.turn_index}')}:turn:{turn.turn_index}"
    commit_hash, err = _try_commit_with_hash(
        exec_ctx.workdir,
        commit_name,
        exec_ctx.options.no_commit,
        stage_all=exec_ctx.options.stage_all,
        approved_paths=approved_path_list if not exec_ctx.options.stage_all else None,
    )
    if err is not None:
        return err
    if commit_hash is None:
        return None

    commit_summary_path = paths.implementation_commit_path(turn.turn_index, turn.owner_title, attempt)
    commit_summary = _render_implementation_commit_artifact(
        phase_label,
        turn,
        approved_paths=approved_path_list,
        commit_hash=commit_hash,
    )
    _write_text_artifact(commit_summary_path, commit_summary)
    _record_implementation_turn_step(
        exec_ctx,
        phase_id=phase_id,
        paths=paths,
        turn=turn,
        step="commit",
        attempt=attempt,
        metadata={
            "approved_paths": approved_path_list,
            "baseline_changed_paths": baseline_changed_paths,
            "commit_hash": commit_hash,
        },
    )
    return None


def _implementation_changed_paths(exec_ctx: PipelineExecutionContext) -> list[str]:
    """Return changed repo paths excluding `.company` artifacts."""
    if not is_git_repo(exec_ctx.workdir):
        return []

    root = repo_root(exec_ctx.workdir)
    company_rel = exec_ctx.company.resolve().relative_to(root.resolve()).as_posix()
    return [
        path
        for path in worktree_changed_paths(exec_ctx.workdir)
        if path != company_rel and not path.startswith(f"{company_rel}/")
    ]


def _render_implementation_scope_artifact(
    phase_label: str,
    turn: PhaseImplementationTurn,
    changed_paths: list[str],
) -> str:
    """Render a Markdown summary of the changed-path evidence for a turn."""
    lines = [
        f"# Scope Evidence: {_turn_label(phase_label, turn)}",
        "",
        f"- **Owner:** {turn.owner_title}",
        f"- **Task IDs:** {', '.join(turn.task_ids)}",
        "",
        "## Changed Paths",
    ]
    if changed_paths:
        lines.extend(f"- {path}" for path in changed_paths)
    else:
        lines.append("- None.")
    lines.append("")
    return "\n".join(lines)


def _implementation_retry_feedback(review: dict[str, object], validation_report: str | None = None) -> str:
    """Return concrete rerun guidance from review and validation output."""
    lines: list[str] = []
    if validation_report is not None:
        lines.extend(
            [
                "Fix the failing command validations before rerunning this same turn.",
                "",
                validation_report,
                "",
            ]
        )

    required_follow_up = review.get("required_follow_up", [])
    if isinstance(required_follow_up, list) and required_follow_up:
        lines.append("Address the following Development Lead follow-up before rerunning this turn:")
        lines.extend(f"- {item}" for item in required_follow_up if isinstance(item, str))
        return "\n".join(lines)

    lines.append(str(review.get("summary", "Revise the turn based on Development Lead feedback.")))
    for field in ("scope_findings", "standards_findings", "validation_findings"):
        findings = review.get(field, [])
        if isinstance(findings, list):
            lines.extend(f"- {item}" for item in findings if isinstance(item, str))
    return "\n".join(lines)


def _run_development_lead_review(
    reviewer: Agent,
    *,
    phase_label: str,
    turn: PhaseImplementationTurn,
    phase_data: dict,
    phase_design_content: str,
    plan_content: str,
    execution_content: str,
    validation_contract: dict,
    validation_report: str,
    scope_artifact: str,
    review_path: Path,
    assigned_standards: str,
) -> tuple[dict[str, object] | None, int | None]:
    """Run strict Development Lead review for one implementation turn."""
    review_context = {
        "implementation_review_request": build_development_lead_review_request(phase_label, turn),
        "current_phase": json.dumps(phase_data, indent=2),
        "phase_design": phase_design_content,
        "scheduled_turn": render_phase_implementation_turn_summary(turn),
        "implementation_plan": plan_content,
        "implementation_execution": execution_content,
        "validation_contract_json": json.dumps(validation_contract, indent=2),
        "validation_contract_markdown": render_validation_contract_markdown(validation_contract),
        "validation_report": validation_report,
        "changed_path_evidence": scope_artifact,
        "assigned_standards": assigned_standards,
    }

    for review_attempt in range(1, _MAX_RETRIES + 2):
        raw_review = _invoke_agent_with_progress(
            reviewer,
            review_context,
            f"{_turn_label(phase_label, turn)} Implementation Review",
            attempt=review_attempt,
        )
        _write_text_artifact(review_path, raw_review)
        errors, review = lint_development_lead_review_json(raw_review)
        if not errors and review is not None:
            return review, None

        if review_attempt > _MAX_RETRIES:
            print(
                f"\nError: Development Lead review output stayed invalid for {_turn_label(phase_label, turn)}.",
                file=sys.stderr,
            )
            return None, 1

        review_context = {
            **review_context,
            "review_format_feedback": "Return valid JSON only and fix these issues:\n"
            + "\n".join(f"- {error}" for error in errors),
            "previous_review_output": raw_review,
        }

    return None, 1


def _run_phase_implementation_turn(  # pylint: disable=too-many-arguments,too-many-locals
    exec_ctx: PipelineExecutionContext,
    *,
    architecture_json: str,
    execution_plan_json: str,
    phase_data: dict,
    phase_label: str,
    phase_design_content: str,
    reviewer_entry: dict,
    paths: PhaseArtifactPaths,
    turn: PhaseImplementationTurn,
    force_rerun_from_plan: bool = False,
) -> int | None:
    """Run plan, execute, validate, review, and commit for one implementation turn."""
    phase_id = str(phase_data.get("id", f"phase_{turn.turn_index}"))
    role_filename = turn.roster_entry.get("filename")
    if not isinstance(role_filename, str) or not role_filename:
        raise RuntimeError(f"Roster entry for {turn.owner_title!r} is missing a role filename.")

    owner_agent = Agent(
        name=turn.owner_title,
        role_file=exec_ctx.company / "roles" / role_filename,
        llm=exec_ctx.llm,
        standards=_standard_paths_for_entry(exec_ctx.company, turn.roster_entry),
    )
    reviewer_agent = Agent(
        name="Development Lead",
        role_file=exec_ctx.company / "roles" / "development_lead.md",
        llm=exec_ctx.llm,
        standards=_standard_paths_for_entry(exec_ctx.company, reviewer_entry),
    )
    assigned_standards = _load_assigned_standards_content(
        exec_ctx.company / "standards",
        turn.roster_entry.get("assigned_standards", []),
    )
    turn_summary = render_phase_implementation_turn_summary(turn)
    current_phase_json = json.dumps(phase_data, indent=2)
    turn_label = _turn_label(phase_label, turn)
    resume_plan, statuses = _classify_implementation_turn_resume(
        exec_ctx,
        phase_id=phase_id,
        paths=paths,
        turn=turn,
        force_rerun_from_plan=force_rerun_from_plan,
    )
    if resume_plan.action == "commit":
        return _commit_implementation_turn(
            exec_ctx,
            phase_id=phase_id,
            phase_data=phase_data,
            phase_label=phase_label,
            paths=paths,
            turn=turn,
            attempt=resume_plan.attempt,
            approved_paths=resume_plan.approved_paths or [],
            baseline_changed_paths=resume_plan.baseline_changed_paths or [],
        )

    if resume_plan.action == "resume" and resume_plan.start_step != "plan":
        print(f"↻ Resuming {turn_label} from the saved {resume_plan.start_step} step.")

    baseline_changed_paths = set(
        resume_plan.baseline_changed_paths
        if resume_plan.baseline_changed_paths is not None
        else _implementation_changed_paths(exec_ctx)
    )
    feedback: str | None = resume_plan.feedback
    plan_path = paths.implementation_plan_path(turn.turn_index, turn.owner_title, resume_plan.attempt)
    execution_path = paths.implementation_execution_path(turn.turn_index, turn.owner_title, resume_plan.attempt)
    validation_path = paths.implementation_validation_path(turn.turn_index, turn.owner_title, resume_plan.attempt)
    scope_path = paths.implementation_scope_path(turn.turn_index, turn.owner_title, resume_plan.attempt)
    review_path = paths.implementation_review_path(turn.turn_index, turn.owner_title, resume_plan.attempt)

    for attempt in range(resume_plan.attempt, _MAX_RETRIES + 2):
        start_step = resume_plan.start_step if attempt == resume_plan.attempt else "plan"
        validation_contract = load_validation_contract(exec_ctx.company) or ensure_validation_contract(exec_ctx.company)
        validation_contract_json = json.dumps(validation_contract, indent=2)
        validation_contract_markdown = render_validation_contract_markdown(validation_contract)
        plan_path = paths.implementation_plan_path(turn.turn_index, turn.owner_title, attempt)
        execution_path = paths.implementation_execution_path(turn.turn_index, turn.owner_title, attempt)
        validation_path = paths.implementation_validation_path(turn.turn_index, turn.owner_title, attempt)
        scope_path = paths.implementation_scope_path(turn.turn_index, turn.owner_title, attempt)
        review_path = paths.implementation_review_path(turn.turn_index, turn.owner_title, attempt)

        if start_step == "plan":
            plan_context = {
                "implementation_plan_request": build_implementation_plan_request(phase_label, turn),
                "architecture": architecture_json,
                "execution_plan": execution_plan_json,
                "current_phase": current_phase_json,
                "phase_design": phase_design_content,
                "scheduled_turn": turn_summary,
                "validation_contract_json": validation_contract_json,
                "validation_contract_markdown": validation_contract_markdown,
            }
            plan_content = _invoke_agent_plan_with_progress(
                owner_agent,
                plan_context,
                f"{turn_label} Implementation Plan",
                feedback=feedback,
                attempt=attempt,
            )
            _write_text_artifact(plan_path, plan_content)
            _record_implementation_turn_step(
                exec_ctx,
                phase_id=phase_id,
                paths=paths,
                turn=turn,
                step="plan",
                attempt=attempt,
                metadata={"baseline_changed_paths": sorted(baseline_changed_paths)},
            )
        else:
            plan_content = plan_path.read_text(encoding="utf-8")

        if start_step in ("plan", "execute"):
            execution_context = {
                "implementation_execute_request": build_implementation_execute_request(phase_label, turn),
                "architecture": architecture_json,
                "execution_plan": execution_plan_json,
                "current_phase": current_phase_json,
                "phase_design": phase_design_content,
                "scheduled_turn": turn_summary,
                "validation_contract_json": validation_contract_json,
                "validation_contract_markdown": validation_contract_markdown,
            }
            execution_content = _invoke_agent_execute_with_progress(
                owner_agent,
                execution_context,
                f"{turn_label} Implementation Execute",
                plan=plan_content,
                feedback=feedback,
                attempt=attempt,
            )
            _write_text_artifact(execution_path, execution_content)
            _record_implementation_turn_step(
                exec_ctx,
                phase_id=phase_id,
                paths=paths,
                turn=turn,
                step="execute",
                attempt=attempt,
                metadata={"baseline_changed_paths": sorted(baseline_changed_paths)},
            )
        else:
            execution_content = execution_path.read_text(encoding="utf-8")

        if start_step in ("plan", "execute", "validate"):
            validation_contract = load_validation_contract(exec_ctx.company) or ensure_validation_contract(
                exec_ctx.company
            )
            validation_report_obj = run_validation_contract(validation_contract, workspace=exec_ctx.workdir)
            validation_report = render_validation_report_markdown(validation_report_obj, report_title=turn_label)
            _write_text_artifact(validation_path, validation_report)

            changed_paths = [
                path for path in _implementation_changed_paths(exec_ctx) if path not in baseline_changed_paths
            ]
            scope_artifact = _render_implementation_scope_artifact(phase_label, turn, changed_paths)
            _write_text_artifact(scope_path, scope_artifact)
            _record_implementation_turn_step(
                exec_ctx,
                phase_id=phase_id,
                paths=paths,
                turn=turn,
                step="validate",
                attempt=attempt,
                metadata={
                    "baseline_changed_paths": sorted(baseline_changed_paths),
                    "passed": validation_report_obj.passed,
                },
            )
        else:
            validation_report = validation_path.read_text(encoding="utf-8")
            scope_artifact = scope_path.read_text(encoding="utf-8")
            validation_report_obj = type(
                "ValidationResult", (), {"passed": statuses["validate"].metadata.get("passed") is True}
            )()

        changed_paths = [path for path in _implementation_changed_paths(exec_ctx) if path not in baseline_changed_paths]

        review, err = _run_development_lead_review(
            reviewer_agent,
            phase_label=phase_label,
            turn=turn,
            phase_data=phase_data,
            phase_design_content=phase_design_content,
            plan_content=plan_content,
            execution_content=execution_content,
            validation_contract=validation_contract,
            validation_report=validation_report,
            scope_artifact=scope_artifact,
            review_path=review_path,
            assigned_standards=assigned_standards,
        )
        if err is not None:
            return err
        assert review is not None  # noqa: S101
        _record_implementation_turn_step(
            exec_ctx,
            phase_id=phase_id,
            paths=paths,
            turn=turn,
            step="review",
            attempt=attempt,
            metadata={
                "baseline_changed_paths": sorted(baseline_changed_paths),
                "decision": review["decision"],
                "summary": review["summary"],
                "scope_findings": review["scope_findings"],
                "standards_findings": review["standards_findings"],
                "validation_findings": review["validation_findings"],
                "required_follow_up": review["required_follow_up"],
                "approved_paths": changed_paths if review["decision"] == "approve" else [],
                "changed_paths": changed_paths,
            },
        )

        if review["decision"] == "approve" and validation_report_obj.passed:
            approved_paths = changed_paths if not exec_ctx.options.stage_all else changed_paths
            return _commit_implementation_turn(
                exec_ctx,
                phase_id=phase_id,
                phase_data=phase_data,
                phase_label=phase_label,
                paths=paths,
                turn=turn,
                attempt=attempt,
                approved_paths=approved_paths,
                baseline_changed_paths=sorted(baseline_changed_paths),
            )

        feedback = _implementation_retry_feedback(
            review,
            validation_report=validation_report if not validation_report_obj.passed else None,
        )
        print(f"↻ Revising {turn_label} based on review feedback.")

    print(
        f"\nError: implementation turn failed after {_MAX_RETRIES + 1} attempts: {_turn_label(phase_label, turn)}",
        file=sys.stderr,
    )
    return 1


def _run_phase_implementation_loop(
    exec_ctx: PipelineExecutionContext,
    *,
    architecture_json: str,
    execution_plan_json: str,
    roster_json: str,
) -> int | None:
    """Run the owner-turn implementation loop for each prepared phase."""
    execution_plan = json.loads(execution_plan_json)
    phases = execution_plan.get("phases", [])
    if not isinstance(phases, list) or not phases:
        return None

    print("\n>> Phase implementation loop")
    for phase_index, phase_data in enumerate(phases):
        if not isinstance(phase_data, dict):
            continue

        label = _phase_label(phase_data, phase_index)
        print(f"\n── Implementing {label} ──")
        paths = build_phase_artifact_paths(exec_ctx.company, phase_index)
        if not paths.final_path.is_file() or not paths.task_mapping_json_path.is_file():
            print(f"\nError: missing phase implementation inputs for {label}.", file=sys.stderr)
            return 1

        phase_design_content = paths.final_path.read_text(encoding="utf-8")
        task_mapping = json.loads(paths.task_mapping_json_path.read_text(encoding="utf-8"))
        team_entries = _phase_team_entries(roster_json, phase_data)
        reviewer_entry = next((entry for entry in team_entries if entry["title"] == "Development Lead"), None)
        if reviewer_entry is None:
            raise RuntimeError(f"Phase team for {label} does not include the Development Lead reviewer.")

        phase_id = str(phase_data.get("id", f"phase_{phase_index + 1}"))
        completed_task_ids: set[str] = set()
        allow_skip = True
        force_downstream_rerun_from_plan = False
        turn_index = 1
        while True:
            turn = next_phase_implementation_turn(
                task_mapping,
                roster_json,
                completed_task_ids=completed_task_ids,
                turn_index=turn_index,
            )
            if turn is None:
                break

            commit_status = _evaluate_implementation_commit_status(
                exec_ctx,
                phase_id=phase_id,
                paths=paths,
                turn=turn,
            )
            if allow_skip and commit_status.is_current:
                print(f"↩ Skipping {_turn_label(label, turn)} (already committed)")
                completed_task_ids.update(turn.task_ids)
                turn_index += 1
                continue

            allow_skip = False

            err = _run_phase_implementation_turn(
                exec_ctx,
                architecture_json=architecture_json,
                execution_plan_json=execution_plan_json,
                phase_data=phase_data,
                phase_label=label,
                phase_design_content=phase_design_content,
                reviewer_entry=reviewer_entry,
                paths=paths,
                turn=turn,
                force_rerun_from_plan=force_downstream_rerun_from_plan,
            )
            if err is not None:
                return err

            completed_task_ids.update(turn.task_ids)
            force_downstream_rerun_from_plan = True
            turn_index += 1

    return None


def _run_or_skip_phase_design_step(  # pylint: disable=too-many-arguments,too-many-locals
    exec_ctx: PipelineExecutionContext,
    *,
    prd_content: str,
    architecture_json: str,
    execution_plan_json: str,
    roster_json: str,
    phase_data: dict,
    phase_index: int,
    paths: PhaseArtifactPaths,
) -> str:
    """Return the final design artifact for one phase, running it only when needed."""
    phase_id = phase_data.get("id", f"phase_{phase_index + 1}")
    phase_key = _phase_loop_state_name(phase_id, "design")
    proposal_key = _phase_loop_state_name(phase_id, "devops-proposal")
    execution_key = _phase_loop_state_name(phase_id, "devops-execution")
    team_entries = _phase_team_entries(roster_json, phase_data)
    allowed_roles = {entry["title"] for entry in team_entries}
    input_paths = _phase_design_input_paths(exec_ctx, team_entries)
    output_paths = _phase_design_output_paths(paths, team_entries)
    label = _phase_label(phase_data, phase_index)
    status = _evaluate_phase_status(
        exec_ctx,
        phase_key,
        input_paths=input_paths,
        output_paths=output_paths,
    )
    status = _sync_saved_phase_design_artifacts(
        exec_ctx,
        status=status,
        phase_key=phase_key,
        input_paths=input_paths,
        output_paths=output_paths,
        paths=paths,
        allowed_roles=allowed_roles,
        phase_label=label,
    )
    if status.is_current:
        _print_skip_message(f"{label} design", status)
        return paths.final_path.read_text(encoding="utf-8")

    if status.has_record:
        if status.changed_inputs and not status.missing_outputs:
            action = _prompt_phase_invalidation(f"{label} design", status)
            if action == "continue":
                print("\n↩ Continuing with saved phase design artifacts despite tracked input changes.")
                return paths.final_path.read_text(encoding="utf-8")
            if action == "restart":
                raise PipelineRestartRequested()
        _print_rerun_reason(f"{label} design", status)

    _clear_phase_markers(exec_ctx.workdir, exec_ctx.state, [proposal_key, execution_key])
    final = _run_phase_design_step(
        exec_ctx.company,
        vision_content=exec_ctx.vision_content,
        prd_content=prd_content,
        architecture_json=architecture_json,
        execution_plan_json=execution_plan_json,
        roster_json=roster_json,
        phase_data=phase_data,
        phase_index=phase_index,
        paths=paths,
        llm=exec_ctx.llm,
    )
    mark_phase_complete(
        exec_ctx.workdir,
        exec_ctx.state,
        phase_key,
        input_paths=input_paths,
        output_paths=output_paths,
    )
    return final


def _devops_proposal_input_paths(exec_ctx: PipelineExecutionContext, paths: PhaseArtifactPaths) -> list[Path]:
    """Return tracked inputs for a DevOps setup proposal."""
    devcontainer_files = [
        exec_ctx.workdir / ".devcontainer" / "post-create.sh",
        exec_ctx.workdir / ".devcontainer" / "post-start.sh",
        exec_ctx.workdir / ".devcontainer" / "devcontainer.json",
    ]
    return [paths.final_path, exec_ctx.company / "roles" / "devops_engineer.md", *devcontainer_files]


def _run_devops_proposal_step(  # pylint: disable=too-many-arguments
    company: Path,
    *,
    phase_data: dict,
    phase_index: int,
    final_design_content: str,
    paths: PhaseArtifactPaths,
    llm: LLMBackend,
    founder_feedback: str | None = None,
) -> str:
    """Generate the DevOps setup proposal and extracted script for one phase."""
    label = _phase_label(phase_data, phase_index)
    devops_engineer = Agent(
        name="DevOps Engineer",
        role_file=company / "roles" / "devops_engineer.md",
        llm=llm,
    )
    devcontainer_dir = company.parent / ".devcontainer"
    post_create = devcontainer_dir / "post-create.sh"
    post_start = devcontainer_dir / "post-start.sh"
    devcontainer_json = devcontainer_dir / "devcontainer.json"
    proposal = _agent_loop(
        devops_engineer,
        {
            "devops_setup_request": _devops_proposal_request(phase_data, paths.script_path),
            "current_phase": json.dumps(phase_data, indent=2),
            "phase_design_final": final_design_content,
            "required_tooling": "\n".join(
                f"- {item}" for item in extract_markdown_list_items(final_design_content, "Required Tooling")
            ),
            "existing_post_create": post_create.read_text(encoding="utf-8") if post_create.is_file() else "(missing)",
            "existing_post_start": post_start.read_text(encoding="utf-8") if post_start.is_file() else "(missing)",
            "existing_devcontainer_json": (
                devcontainer_json.read_text(encoding="utf-8") if devcontainer_json.is_file() else "(missing)"
            ),
        },
        lambda content: lint_devops_proposal(content)[0],
        f"{label} DevOps Proposal",
        founder_feedback=founder_feedback,
    )
    _, script = lint_devops_proposal(proposal)
    assert script is not None  # noqa: S101
    _write_text_artifact(paths.proposal_path, proposal)
    _write_text_artifact(paths.summary_path, render_setup_summary(proposal, paths.script_path))
    _write_text_artifact(paths.script_path, script + "\n")
    print(f"\n✓ DevOps setup proposal written: {paths.proposal_path}")
    print(f"✓ DevOps setup summary written: {paths.summary_path}")
    print(f"✓ DevOps setup script written: {paths.script_path}")
    return proposal


def _run_or_skip_devops_proposal_step(
    exec_ctx: PipelineExecutionContext,
    *,
    phase_data: dict,
    phase_index: int,
    final_design_content: str,
    paths: PhaseArtifactPaths,
) -> str:
    """Return the DevOps setup proposal, running it only when needed."""
    phase_id = phase_data.get("id", f"phase_{phase_index + 1}")
    phase_key = _phase_loop_state_name(phase_id, "devops-proposal")
    execution_key = _phase_loop_state_name(phase_id, "devops-execution")
    input_paths = _devops_proposal_input_paths(exec_ctx, paths)
    output_paths = [paths.proposal_path, paths.summary_path, paths.script_path]
    status = _evaluate_phase_status(exec_ctx, phase_key, input_paths=input_paths, output_paths=output_paths)
    if status.is_current:
        _print_skip_message(f"{_phase_label(phase_data, phase_index)} DevOps proposal", status)
        return paths.proposal_path.read_text(encoding="utf-8")

    if status.has_record:
        if status.changed_inputs and not status.missing_outputs:
            action = _prompt_phase_invalidation(f"{_phase_label(phase_data, phase_index)} DevOps proposal", status)
            if action == "continue":
                print("\n↩ Continuing with saved DevOps proposal artifacts despite tracked input changes.")
                return paths.proposal_path.read_text(encoding="utf-8")
            if action == "restart":
                raise PipelineRestartRequested()
        _print_rerun_reason(f"{_phase_label(phase_data, phase_index)} DevOps proposal", status)

    _clear_phase_marker(exec_ctx.workdir, exec_ctx.state, execution_key)
    proposal = _run_devops_proposal_step(
        exec_ctx.company,
        phase_data=phase_data,
        phase_index=phase_index,
        final_design_content=final_design_content,
        paths=paths,
        llm=exec_ctx.llm,
    )
    mark_phase_complete(exec_ctx.workdir, exec_ctx.state, phase_key, input_paths=input_paths, output_paths=output_paths)
    return proposal


def _run_or_skip_devops_execution_step(  # pylint: disable=too-many-locals
    exec_ctx: PipelineExecutionContext,
    *,
    phase_data: dict,
    phase_index: int,
    final_design_content: str,
    paths: PhaseArtifactPaths,
) -> int | None:
    """Run the Founder-gated DevOps setup script for one phase when needed."""
    phase_id = phase_data.get("id", f"phase_{phase_index + 1}")
    proposal_key = _phase_loop_state_name(phase_id, "devops-proposal")
    execution_key = _phase_loop_state_name(phase_id, "devops-execution")
    input_paths = [paths.proposal_path, paths.summary_path, paths.script_path]
    status = _evaluate_phase_status(exec_ctx, execution_key, input_paths=input_paths, output_paths=[])
    label = _phase_label(phase_data, phase_index)
    if status.is_current:
        _print_skip_message(f"{label} DevOps execution", status)
        return None

    if status.has_record:
        _print_rerun_reason(f"{label} DevOps execution", status)

    if not exec_ctx.options.execute_phase_setups:
        mark_phase_complete(
            exec_ctx.workdir,
            exec_ctx.state,
            execution_key,
            input_paths=input_paths,
            output_paths=[],
            metadata={
                "status": "deferred",
                "reason": _PHASE_SETUP_EXECUTION_DEFERRED_REASON,
                "proposal_checksum": hash_file(paths.proposal_path),
                "summary_checksum": hash_file(paths.summary_path),
                "script_checksum": hash_file(paths.script_path),
            },
        )
        print(
            f"\n↩ Deferred DevOps execution for {label}. "
            "Recorded the generated setup artifacts without running the script because phase setup "
            "execution is not enabled in this pipeline yet."
        )
        return None

    attempt = 1
    while True:
        approval = founder_approve_devops_execution(label, paths.proposal_path, script_path=paths.script_path)
        if approval.action == "revise":
            proposal = _run_devops_proposal_step(
                exec_ctx.company,
                phase_data=phase_data,
                phase_index=phase_index,
                final_design_content=final_design_content,
                paths=paths,
                llm=exec_ctx.llm,
                founder_feedback=approval.feedback,
            )
            mark_phase_complete(
                exec_ctx.workdir,
                exec_ctx.state,
                proposal_key,
                input_paths=_devops_proposal_input_paths(exec_ctx, paths),
                output_paths=[paths.proposal_path, paths.summary_path, paths.script_path],
            )
            logger.debug("Founder requested DevOps proposal revision; regenerated proposal (%s chars)", len(proposal))
            continue

        print(f"\n>> Executing approved DevOps script for {label} (attempt {attempt}/3)")
        before_snapshot = snapshot_tracked_repo_files(exec_ctx.workdir)
        result = subprocess.run(
            ["bash", str(paths.script_path)],
            cwd=exec_ctx.workdir,
            capture_output=True,
            text=True,
            check=False,
        )
        after_snapshot = snapshot_tracked_repo_files(exec_ctx.workdir)
        tracked_mutations = find_tracked_file_mutations(before_snapshot, after_snapshot)

        log_lines = [
            f"# DevOps Setup Attempt {attempt}",
            "",
            f"- Phase: {label}",
            f"- Exit Code: {result.returncode}",
            f"- Script: {paths.script_path}",
            "",
            "## STDOUT",
            "",
            "```text",
            result.stdout,
            "```",
            "",
            "## STDERR",
            "",
            "```text",
            result.stderr,
            "```",
            "",
        ]
        if tracked_mutations:
            log_lines.extend(
                [
                    "## Tracked File Mutations",
                    "",
                    *[f"- {path}" for path in tracked_mutations],
                    "",
                ]
            )

        attempt_log_path = paths.attempt_log_path(attempt)
        _write_text_artifact(attempt_log_path, "\n".join(log_lines))
        print(f"✓ DevOps attempt log written: {attempt_log_path}")

        if tracked_mutations:
            print("\nError: DevOps script modified tracked repository files outside the approved artifact boundary.")
            for path in tracked_mutations:
                print(f"  - {path}")
            print("  → Stopping for human review instead of retrying automatically.")
            return 1

        if result.returncode == 0:
            mark_phase_complete(
                exec_ctx.workdir,
                exec_ctx.state,
                execution_key,
                input_paths=input_paths,
                output_paths=[],
                metadata={
                    "approved_proposal_checksum": hash_file(paths.proposal_path),
                    "script_checksum": hash_file(paths.script_path),
                    "attempts": attempt,
                    "last_attempt_log": str(attempt_log_path),
                },
            )
            print(f"✓ DevOps execution completed for {label}")
            return None

        if attempt >= 3:
            print(f"\nError: DevOps setup failed for {label} after 3 approved attempts.", file=sys.stderr)
            return 1

        feedback = (
            "Revise the DevOps setup proposal to address the failed execution log below. "
            "Preserve the same safety constraints, keep the script non-interactive, and change only what is needed.\n\n"
            f"STDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}"
        )
        _run_devops_proposal_step(
            exec_ctx.company,
            phase_data=phase_data,
            phase_index=phase_index,
            final_design_content=final_design_content,
            paths=paths,
            llm=exec_ctx.llm,
            founder_feedback=feedback,
        )
        mark_phase_complete(
            exec_ctx.workdir,
            exec_ctx.state,
            proposal_key,
            input_paths=_devops_proposal_input_paths(exec_ctx, paths),
            output_paths=[paths.proposal_path, paths.summary_path, paths.script_path],
        )
        attempt += 1


def _run_phase_preparation_loop(
    exec_ctx: PipelineExecutionContext,
    *,
    prd_content: str,
    architecture_json: str,
    execution_plan_json: str,
    roster_json: str,
) -> int | None:
    """Run guarded phase-design and DevOps setup preparation for each phase."""
    execution_plan = json.loads(execution_plan_json)
    phases = execution_plan.get("phases", [])
    if not isinstance(phases, list) or not phases:
        return None

    print("\n>> Phase preparation loop")
    for phase_index, phase_data in enumerate(phases):
        if not isinstance(phase_data, dict):
            continue
        label = _phase_label(phase_data, phase_index)
        print(f"\n── Preparing {label} ──")
        paths = build_phase_artifact_paths(exec_ctx.company, phase_index)
        final_design = _run_or_skip_phase_design_step(
            exec_ctx,
            prd_content=prd_content,
            architecture_json=architecture_json,
            execution_plan_json=execution_plan_json,
            roster_json=roster_json,
            phase_data=phase_data,
            phase_index=phase_index,
            paths=paths,
        )
        _run_or_skip_devops_proposal_step(
            exec_ctx,
            phase_data=phase_data,
            phase_index=phase_index,
            final_design_content=final_design,
            paths=paths,
        )
        err = _run_or_skip_devops_execution_step(
            exec_ctx,
            phase_data=phase_data,
            phase_index=phase_index,
            final_design_content=final_design,
            paths=paths,
        )
        if err is not None:
            return err
    return None


def _run_or_skip_prd_phase(exec_ctx: PipelineExecutionContext) -> tuple[str, int | None]:
    """Return PRD content, running the phase only when needed."""
    prd_path = exec_ctx.company / "artifacts" / "prd.md"
    prd_inputs = [
        exec_ctx.vision_path,
        exec_ctx.company / "roles" / "cpo.md",
    ]
    status = _evaluate_phase_status(exec_ctx, "prd", input_paths=prd_inputs, output_paths=[prd_path])
    if status.is_current:
        _print_skip_message("PRD", status)
        prd_content = prd_path.read_text(encoding="utf-8")
        err = _ensure_commit_complete(exec_ctx, "prd-generation")
        return prd_content, err

    if status.has_record:
        if status.changed_inputs and not status.missing_outputs:
            action = _prompt_phase_invalidation("PRD", status)
            if action == "continue":
                print("\n↩ Continuing with saved PRD artifacts despite tracked input changes.")
                prd_content = prd_path.read_text(encoding="utf-8")
                err = _ensure_commit_complete(exec_ctx, "prd-generation")
                return prd_content, err
            if action == "restart":
                raise PipelineRestartRequested()
        _print_rerun_reason("PRD", status)

    _clear_phase_markers(
        exec_ctx.workdir,
        exec_ctx.state,
        [
            _commit_phase_name("prd-generation"),
            "architecture",
            "execution_plan",
            "roster",
            "roles",
            _commit_phase_name("architecture-generation"),
            _commit_phase_name("execution-plan-generation"),
            _commit_phase_name("hiring"),
        ],
    )
    prd_content = _run_prd_phase(exec_ctx.company, exec_ctx.vision_content, exec_ctx.llm)
    mark_phase_complete(exec_ctx.workdir, exec_ctx.state, "prd", input_paths=prd_inputs, output_paths=[prd_path])
    err = _ensure_commit_complete(exec_ctx, "prd-generation")
    return prd_content, err


def _run_or_skip_architecture_phase(
    exec_ctx: PipelineExecutionContext,
    prd_content: str,
) -> tuple[str, int | None]:
    """Return architecture JSON, running the phase only when needed."""
    arch_json_path = exec_ctx.company / "artifacts" / "architecture.json"
    arch_md_path = exec_ctx.company / "artifacts" / "architecture.md"
    prd_path = exec_ctx.company / "artifacts" / "prd.md"
    arch_inputs = [
        exec_ctx.vision_path,
        prd_path,
        exec_ctx.company / "roles" / "cto.md",
    ]
    status = _evaluate_phase_status(
        exec_ctx,
        "architecture",
        input_paths=arch_inputs,
        output_paths=[arch_json_path, arch_md_path],
    )
    if status.is_current:
        _print_skip_message("Architecture", status)
        arch_json = arch_json_path.read_text(encoding="utf-8")
        err = _ensure_commit_complete(exec_ctx, "architecture-generation")
        return arch_json, err

    if status.has_record:
        if status.changed_inputs and not status.missing_outputs:
            action = _prompt_phase_invalidation("Architecture", status)
            if action == "continue":
                print("\n↩ Continuing with saved Architecture artifacts despite tracked input changes.")
                arch_json = arch_json_path.read_text(encoding="utf-8")
                err = _ensure_commit_complete(exec_ctx, "architecture-generation")
                return arch_json, err
            if action == "restart":
                raise PipelineRestartRequested()
        _print_rerun_reason("Architecture", status)

    _clear_phase_markers(
        exec_ctx.workdir,
        exec_ctx.state,
        [
            _commit_phase_name("architecture-generation"),
            "execution_plan",
            "roster",
            "roles",
            _commit_phase_name("execution-plan-generation"),
            _commit_phase_name("hiring"),
        ],
    )
    arch_json = _run_architecture_phase(exec_ctx.company, exec_ctx.vision_content, prd_content, exec_ctx.llm)
    mark_phase_complete(
        exec_ctx.workdir,
        exec_ctx.state,
        "architecture",
        input_paths=arch_inputs,
        output_paths=[arch_json_path, arch_md_path],
    )
    err = _ensure_commit_complete(exec_ctx, "architecture-generation")
    return arch_json, err


def _run_or_skip_execution_plan_phase(
    exec_ctx: PipelineExecutionContext,
    prd_content: str,
    arch_json: str,
) -> tuple[str, int | None]:
    """Return execution-plan JSON, running the phase only when needed."""
    plan_json_path = exec_ctx.company / "artifacts" / "execution_plan.json"
    plan_md_path = exec_ctx.company / "artifacts" / "execution_plan.md"
    prd_path = exec_ctx.company / "artifacts" / "prd.md"
    arch_json_path = exec_ctx.company / "artifacts" / "architecture.json"
    plan_inputs = [
        exec_ctx.vision_path,
        prd_path,
        arch_json_path,
        exec_ctx.company / "roles" / "vpe.md",
        exec_ctx.company / "templates" / "execution_plan_template.md",
    ]
    status = _evaluate_phase_status(
        exec_ctx,
        "execution_plan",
        input_paths=plan_inputs,
        output_paths=[plan_json_path, plan_md_path],
    )
    if status.is_current:
        _print_skip_message("Execution Plan", status)
        plan_json = plan_json_path.read_text(encoding="utf-8")
        err = _ensure_commit_complete(exec_ctx, "execution-plan-generation")
        return plan_json, err

    if status.has_record:
        if status.changed_inputs and not status.missing_outputs:
            action = _prompt_phase_invalidation("Execution Plan", status)
            if action == "continue":
                print("\n↩ Continuing with saved Execution Plan artifacts despite tracked input changes.")
                plan_json = plan_json_path.read_text(encoding="utf-8")
                err = _ensure_commit_complete(exec_ctx, "execution-plan-generation")
                return plan_json, err
            if action == "restart":
                raise PipelineRestartRequested()
        _print_rerun_reason("Execution Plan", status)

    _clear_phase_markers(
        exec_ctx.workdir,
        exec_ctx.state,
        [
            _commit_phase_name("execution-plan-generation"),
            "roster",
            "roles",
            _commit_phase_name("hiring"),
        ],
    )
    plan_json = _run_execution_plan_phase(
        exec_ctx.company,
        exec_ctx.vision_content,
        prd_content,
        arch_json,
        exec_ctx.llm,
    )
    mark_phase_complete(
        exec_ctx.workdir,
        exec_ctx.state,
        "execution_plan",
        input_paths=plan_inputs,
        output_paths=[plan_json_path, plan_md_path],
    )
    err = _ensure_commit_complete(exec_ctx, "execution-plan-generation")
    return plan_json, err


def _run_or_skip_roster_phase(exec_ctx: PipelineExecutionContext, arch_json: str, execution_plan_json: str) -> str:
    """Return roster JSON, running the phase only when needed."""
    roster_json_path = exec_ctx.company / "artifacts" / "roster.json"
    roster_md_path = exec_ctx.company / "artifacts" / "roster.md"
    arch_json_path = exec_ctx.company / "artifacts" / "architecture.json"
    plan_json_path = exec_ctx.company / "artifacts" / "execution_plan.json"
    roster_inputs = [
        arch_json_path,
        plan_json_path,
        exec_ctx.company / "roles" / "hiring_manager.md",
        *_available_standard_paths(exec_ctx.company),
    ]
    status = _evaluate_phase_status(
        exec_ctx,
        "roster",
        input_paths=roster_inputs,
        output_paths=[roster_json_path, roster_md_path],
    )
    if status.is_current:
        _print_skip_message("Roster", status)
        return roster_json_path.read_text(encoding="utf-8")

    if status.has_record:
        if status.changed_inputs and not status.missing_outputs:
            action = _prompt_phase_invalidation("Roster", status)
            if action == "continue":
                print("\n↩ Continuing with saved Roster artifacts despite tracked input changes.")
                return roster_json_path.read_text(encoding="utf-8")
            if action == "restart":
                raise PipelineRestartRequested()
        _print_rerun_reason("Roster", status)

    _clear_phase_markers(
        exec_ctx.workdir,
        exec_ctx.state,
        [
            "roles",
            _commit_phase_name("hiring"),
        ],
    )
    roster_json = _run_roster_phase(exec_ctx.company, arch_json, execution_plan_json, exec_ctx.llm)
    mark_phase_complete(
        exec_ctx.workdir,
        exec_ctx.state,
        "roster",
        input_paths=roster_inputs,
        output_paths=[roster_json_path, roster_md_path],
    )
    return roster_json


def _run_or_skip_role_generation_phase(
    exec_ctx: PipelineExecutionContext,
    arch_json: str,
    execution_plan_json: str,
    roster_json: str,
) -> int | None:
    """Run role generation when needed and validate expected files."""
    role_paths = _expected_role_paths(exec_ctx.company, roster_json)
    arch_json_path = exec_ctx.company / "artifacts" / "architecture.json"
    plan_json_path = exec_ctx.company / "artifacts" / "execution_plan.json"
    roster_json_path = exec_ctx.company / "artifacts" / "roster.json"
    role_inputs = [
        arch_json_path,
        plan_json_path,
        roster_json_path,
        exec_ctx.company / "roles" / "role_writer.md",
        exec_ctx.company / "templates" / "role_template.md",
        *_assigned_standard_paths(exec_ctx.company, roster_json),
    ]
    status = _evaluate_phase_status(exec_ctx, "roles", input_paths=role_inputs, output_paths=role_paths)
    if role_paths and status.is_current:
        _print_skip_message("Role Generation", status)
        return None

    if status.has_record:
        if status.changed_inputs and not status.missing_outputs:
            action = _prompt_phase_invalidation("Role Generation", status)
            if action == "continue":
                print("\n↩ Continuing with saved Role Generation artifacts despite tracked input changes.")
                return None
            if action == "restart":
                raise PipelineRestartRequested()
        _print_rerun_reason("Role Generation", status)

    _clear_phase_marker(exec_ctx.workdir, exec_ctx.state, _commit_phase_name("hiring"))
    _run_role_generation(exec_ctx.company, arch_json, execution_plan_json, roster_json, exec_ctx.llm)
    role_paths = _expected_role_paths(exec_ctx.company, roster_json)
    if not role_paths or not all(path.is_file() for path in role_paths):
        print("\nError: role generation completed without writing all expected role files.", file=sys.stderr)
        return 1

    mark_phase_complete(
        exec_ctx.workdir,
        exec_ctx.state,
        "roles",
        input_paths=role_inputs,
        output_paths=role_paths,
    )
    return None


def _is_phase_done(state: dict | None, phase: str, artifact_paths: list[Path], *, workdir: Path) -> bool:
    """Check if *phase* completed previously and its artifacts still exist."""
    record = _phase_record(state, phase)
    completed_at = record.get("completed_at") if isinstance(record.get("completed_at"), str) else None
    if completed_at is None:
        return False

    if not artifact_paths:
        return True

    recorded_outputs = record.get("outputs", {}) if isinstance(record.get("outputs"), dict) else {}
    current_outputs = snapshot_paths(workdir, artifact_paths)
    return all(digest is not None and recorded_outputs.get(path) == digest for path, digest in current_outputs.items())


def _try_commit(
    workdir: Path,
    phase_name: str,
    no_commit: bool,
    *,
    stage_all: bool,
    approved_paths: list[str] | None = None,
) -> int | None:
    """Attempt a git commit.  Returns an error code on failure, else *None*."""
    _commit_hash, err = _try_commit_with_hash(
        workdir,
        phase_name,
        no_commit,
        stage_all=stage_all,
        approved_paths=approved_paths,
    )
    return err


def _try_commit_with_hash(
    workdir: Path,
    phase_name: str,
    no_commit: bool,
    *,
    stage_all: bool,
    approved_paths: list[str] | None = None,
) -> tuple[str | None, int | None]:
    """Attempt a git commit and return ``(commit_hash, err)``."""
    if no_commit:
        print("  (skipping git commit – --no-commit)")
        return None, None
    try:
        commit_hash = commit_state(
            workdir,
            phase_name,
            stage_all=stage_all,
            approved_paths=None if stage_all else approved_paths,
        )
    except GitError as exc:
        print(f"\nError committing {phase_name}: {exc}", file=sys.stderr)
        return None, 1
    return commit_hash, None


def _init_pipeline_state(workdir: Path) -> tuple[dict, Path]:
    """Load or create pipeline state and return ``(state, company)``."""
    company = init_company(workdir)
    ensure_validation_contract(company)
    print(f"\n✓ Company directory initialised: {company}")

    state = read_pipeline_state(workdir)
    if state is None:
        state = new_pipeline_state()
    write_pipeline_state(workdir, state)
    return state, company


def _run_phases(exec_ctx: PipelineExecutionContext) -> int:  # pylint: disable=too-many-return-statements
    """Execute or skip each pipeline phase based on *state*."""
    prd_content, err = _run_or_skip_prd_phase(exec_ctx)
    if err is not None:
        return err

    arch_json, err = _run_or_skip_architecture_phase(exec_ctx, prd_content)
    if err is not None:
        return err

    execution_plan_json, err = _run_or_skip_execution_plan_phase(exec_ctx, prd_content, arch_json)
    if err is not None:
        return err

    roster_json = _run_or_skip_roster_phase(exec_ctx, arch_json, execution_plan_json)
    err = _run_or_skip_role_generation_phase(exec_ctx, arch_json, execution_plan_json, roster_json)
    if err is not None:
        return err

    err = _ensure_commit_complete(exec_ctx, "hiring")
    if err is not None:
        return err

    err = _run_phase_preparation_loop(
        exec_ctx,
        prd_content=prd_content,
        architecture_json=arch_json,
        execution_plan_json=execution_plan_json,
        roster_json=roster_json,
    )
    if err is not None:
        return err

    err = _run_phase_implementation_loop(
        exec_ctx,
        architecture_json=arch_json,
        execution_plan_json=execution_plan_json,
        roster_json=roster_json,
    )
    if err is not None:
        return err

    return 0


def run_pipeline(
    *,
    vision_path: Path,
    workdir: Path,
    options: PipelineRunOptions | None = None,
) -> int:
    """Execute the full V0.2 SDLC pipeline."""
    options = options or PipelineRunOptions()
    logger.debug(
        "run_pipeline called: vision_path=%s workdir=%s no_commit=%s stage_all=%s debug=%s restart=%s",
        vision_path,
        workdir,
        options.no_commit,
        options.stage_all,
        options.debug,
        options.restart,
    )
    print("=" * 72)
    print("  AgenticOrg CLI – V0.3 Pipeline")
    print("=" * 72)

    # 0. Validate git repo early (unless commits are disabled).
    if not options.no_commit and not is_git_repo(workdir):
        print(f"\nError: {workdir} is not inside a git repository.", file=sys.stderr)
        print(
            "  Run: git init && git commit --allow-empty -m 'Initial commit'",
            file=sys.stderr,
        )
        print(
            "  Or skip git entirely: asw start --vision <file> --no-commit",
            file=sys.stderr,
        )
        return 1

    # 0b. Handle --restart: wipe .company/ before anything else.
    if options.restart:
        clear_company(workdir)
        print("\n✓ Restart: .company/ directory removed.")

    # 1. Read vision.
    vision_content = vision_path.read_text(encoding="utf-8")
    logger.debug(
        "Vision loaded from %s (%d chars, sha256=%s)",
        vision_path,
        len(vision_content),
        string_checksum_prefix(vision_content),
    )
    print(f"✓ Vision loaded: {vision_path.name} ({len(vision_content)} chars)")

    # 2. Get LLM backend.
    llm: LLMBackend = get_backend("gemini")
    logger.debug("LLM backend acquired: gemini")
    print("✓ LLM backend: Gemini CLI")

    while True:
        # 3. Load/create pipeline state.
        state, company = _init_pipeline_state(workdir)

        # 4. Execute phases.
        exec_ctx = PipelineExecutionContext(
            state=state,
            company=company,
            vision_path=vision_path,
            vision_content=vision_content,
            llm=llm,
            options=options,
        )

        try:
            result = _run_phases(exec_ctx)
        except PipelineRestartRequested:
            clear_company(workdir)
            print("\n✓ Restart: .company/ directory removed.")
            continue

        if result != 0:
            return result
        break

    # ── Done ─────────────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("  Pipeline complete.")
    print("=" * 72)
    return 0
