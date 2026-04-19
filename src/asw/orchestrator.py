"""Pipeline orchestrator – the main SDLC loop."""

# pylint: disable=too-many-lines

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from asw.agents.base import Agent
from asw.company import (
    clear_company,
    init_company,
    mark_phase_complete,
    new_pipeline_state,
    read_pipeline_state,
    snapshot_paths,
    write_failed_artifact,
    write_pipeline_state,
)
from asw.execution_plan import _lint_execution_plan, _write_execution_plan
from asw.founder_questions import (
    _apply_founder_answers_to_content,
    _apply_founder_answers_to_prd,
    _extract_answered_founder_questions,
    _extract_founder_question_items,
    _extract_founder_questions,
    _render_founder_question_section,
)
from asw.gates import FounderReviewResult, founder_review
from asw.git import GitError, commit_state, is_git_repo
from asw.hiring import _expected_role_paths, _lint_roster, _write_roster
from asw.linters.json_lint import validate_architecture
from asw.linters.markdown import validate_checklist, validate_mermaid, validate_sections
from asw.llm.backend import LLMBackend, get_backend
from asw.llm.errors import LLMInvocationError
from asw.pipeline import PipelineExecutionContext, PipelineRunOptions, string_checksum_prefix

_MAX_RETRIES = 2
_REQUEST_MORE_QUESTIONS_FEEDBACK = (
    "Review the current artifact, preserve all founder decisions already captured, and ask at least one new "
    "founder question whenever meaningful unresolved issues remain."
)
_REQUEST_MORE_QUESTIONS_ESCALATION = (
    "The founder explicitly requested another question round. Return at least one new unresolved founder "
    "question that is not already answered when any meaningful ambiguity remains."
)

logger = logging.getLogger("asw.orchestrator")


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
    for phase in phases:
        _clear_phase_marker(workdir, state, phase)


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
        print(f"\n>> {agent.name} – attempt {attempt}")
        print(
            f"   Invoking {agent.name} via Gemini CLI (may take up to 5 min)…",
            flush=True,
        )
        try:
            output = agent.run(context, feedback=feedback)
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
        print("   Response received.")

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
                review = founder_review("Execution Plan", plan_md_path)
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

    role_path = company / "roles" / entry["filename"]
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


def _try_commit(workdir: Path, phase_name: str, no_commit: bool, *, stage_all: bool) -> int | None:
    """Attempt a git commit.  Returns an error code on failure, else *None*."""
    if no_commit:
        print("  (skipping git commit – --no-commit)")
        return None
    try:
        commit_state(workdir, phase_name, stage_all=stage_all)
    except GitError as exc:
        print(f"\nError committing {phase_name}: {exc}", file=sys.stderr)
        return 1
    return None


def _init_pipeline_state(workdir: Path) -> tuple[dict, Path]:
    """Load or create pipeline state and return ``(state, company)``."""
    company = init_company(workdir)
    print(f"\n✓ Company directory initialised: {company}")

    state = read_pipeline_state(workdir)
    if state is None:
        state = new_pipeline_state()
    write_pipeline_state(workdir, state)
    return state, company


def _run_phases(exec_ctx: PipelineExecutionContext) -> int:
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
    print("  AgenticOrg CLI – V0.2 Pipeline")
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
