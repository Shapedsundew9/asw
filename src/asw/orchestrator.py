"""Pipeline orchestrator – the main SDLC loop."""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Callable
from pathlib import Path

from asw.agents.base import Agent
from asw.company import (
    clear_company,
    hash_file,
    init_company,
    mark_phase_complete,
    read_pipeline_state,
    write_failed_artifact,
    write_pipeline_state,
)
from asw.founder_questions import (
    _apply_founder_answers_to_content,
    _apply_founder_answers_to_prd,
    _extract_answered_founder_questions,
    _extract_founder_questions,
    _render_founder_question_section,
)
from asw.gates import FounderReviewResult, founder_review
from asw.git import GitError, commit_state, is_git_repo
from asw.linters.json_lint import validate_architecture
from asw.linters.markdown import validate_checklist, validate_mermaid, validate_sections
from asw.llm.backend import LLMBackend, get_backend
from asw.llm.errors import LLMInvocationError
from asw.pipeline import PipelineExecutionContext, PipelineRunOptions, string_checksum_prefix

_MAX_RETRIES = 2
_REQUEST_MORE_QUESTIONS_FEEDBACK = (
    "Review the current artifact, preserve all founder decisions already captured, and ask additional "
    "founder questions only if critical unresolved issues remain."
)

logger = logging.getLogger("asw.orchestrator")


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


# ── Roster (Phase C1) helpers ────────────────────────────────────────────

_ROSTER_FILENAME_RE = re.compile(r"^[a-z][a-z0-9_]*\.md$")
_ROSTER_REQUIRED_KEYS = {"title", "filename", "responsibility", "assigned_standards"}


def _lint_roster_entry(entry: dict, prefix: str, available: set[str] | None, errors: list[str]) -> None:
    """Validate a single roster entry and append any errors found."""
    if not isinstance(entry["title"], str) or not entry["title"]:
        errors.append(f"{prefix}.title: must be a non-empty string.")
    if not isinstance(entry["filename"], str) or not _ROSTER_FILENAME_RE.match(entry["filename"]):
        errors.append(f"{prefix}.filename: must match lowercase_underscore.md " f"(got '{entry.get('filename', '')}')")
    if not isinstance(entry["responsibility"], str) or not entry["responsibility"]:
        errors.append(f"{prefix}.responsibility: must be a non-empty string.")
    if not isinstance(entry["assigned_standards"], list):
        errors.append(f"{prefix}.assigned_standards: must be an array.")
    elif available is not None:
        for std in entry["assigned_standards"]:
            if std not in available:
                errors.append(f"{prefix}.assigned_standards: '{std}' not found in standards directory.")


def _lint_roster(content: str, *, standards_dir: Path | None = None) -> list[str]:
    """Validate Hiring Manager roster output.

    Parameters
    ----------
    content:
        Raw LLM output containing a fenced JSON block.
    standards_dir:
        Path to ``.company/standards/`` for validating ``assigned_standards``
        entries.  When *None*, standards references are not checked.
    """
    errors: list[str] = []

    json_block = _extract_json_block(content)
    if json_block is None:
        errors.append("No fenced ```json``` code block found in Hiring Manager output.")
        return errors

    try:
        data = json.loads(json_block)
    except json.JSONDecodeError as exc:
        errors.append(f"JSON parse error: {exc}")
        return errors

    if not isinstance(data, dict) or "hired_agents" not in data:
        errors.append("JSON must be an object with a 'hired_agents' key.")
        return errors

    agents = data["hired_agents"]
    if not isinstance(agents, list) or len(agents) == 0:
        errors.append("'hired_agents' must be a non-empty array.")
        return errors

    available: set[str] | None = None
    if standards_dir is not None and standards_dir.is_dir():
        available = {f.name for f in standards_dir.iterdir() if f.is_file()}

    for idx, entry in enumerate(agents):
        prefix = f"hired_agents[{idx}]"
        if not isinstance(entry, dict):
            errors.append(f"{prefix}: must be an object.")
            continue
        missing = _ROSTER_REQUIRED_KEYS - set(entry.keys())
        if missing:
            errors.append(f"{prefix}: missing keys: {', '.join(sorted(missing))}")
            continue

        _lint_roster_entry(entry, prefix, available, errors)

    logger.debug("Roster lint result: %d error(s)", len(errors))
    for err in errors:
        logger.debug("  Roster lint error: %s", err)
    return errors


def _render_roster_markdown(json_str: str) -> str:
    """Render a human-readable Markdown table from roster JSON."""
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        return "# Proposed Roster\n\n> **Warning:** Roster JSON could not be parsed.\n"

    agents = data.get("hired_agents", [])
    lines = [
        "# Proposed Roster",
        "",
        "> **Source of Truth:** The roster specification is stored in `roster.json`.",
        "",
        "| # | Title | Filename | Responsibility | Standards |",
        "| --- | --- | --- | --- | --- |",
    ]
    for idx, agent in enumerate(agents, 1):
        stds = _safe_join(agent.get("assigned_standards", [])) or "None"
        lines.append(
            f"| {idx} | {agent.get('title', 'N/A')} "
            f"| {agent.get('filename', 'N/A')} "
            f"| {agent.get('responsibility', 'N/A')} "
            f"| {stds} |"
        )
    lines.append("")
    lines.append(f"**Total: {len(agents)} role(s) proposed**")
    founder_questions = data.get("founder_questions", [])
    if isinstance(founder_questions, list) and founder_questions:
        lines.append("")
        lines.extend(_render_founder_question_section(founder_questions, heading="## Founder Input"))
    return "\n".join(lines)


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

        raw_arch = _agent_loop(
            cto,
            rerun_context,
            lambda c: _lint_architecture(c)[0],
            "Architecture",
            founder_feedback=_review_feedback(review),
        )
        _write_architecture(raw_arch, company)
        review = founder_review("Architecture", arch_md_path, questions=_extract_founder_questions(raw_arch))

    # Return the architecture JSON for downstream phases.
    arch_json_path = company / "artifacts" / "architecture.json"
    return arch_json_path.read_text(encoding="utf-8")


def _write_roster(roster_json_str: str, company: Path) -> None:
    """Write roster artifacts to .company/artifacts/."""
    roster_json_path = company / "artifacts" / "roster.json"
    roster_json_path.write_text(roster_json_str, encoding="utf-8")

    roster_md_path = company / "artifacts" / "roster.md"
    roster_md_path.write_text(_render_roster_markdown(roster_json_str), encoding="utf-8")

    print(f"\n✓ Roster JSON written: {roster_json_path}")
    print(f"✓ Roster summary written: {roster_md_path}")


def _run_roster_phase(company: Path, architecture_json: str, llm: LLMBackend) -> str:
    """Run the Hiring Manager roster phase including founder review loop.

    Returns the approved roster JSON string.
    """
    hm = Agent(
        name="Hiring Manager",
        role_file=company / "roles" / "hiring_manager.md",
        llm=llm,
    )
    standards_dir = company / "standards"

    # Build list of available standards filenames.
    available = sorted(f.name for f in standards_dir.iterdir() if f.is_file()) if standards_dir.is_dir() else []

    context = {
        "architecture": architecture_json,
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

    roster_md_path = company / "artifacts" / "roster.md"
    review = founder_review("Roster", roster_md_path, questions=_extract_founder_questions(raw_roster))
    while True:
        if review.action == "approve":
            assert json_block is not None  # noqa: S101
            return json_block

        if review.action == "answer_questions":
            logger.debug("Applying %d founder answer(s) locally to roster", len(review.answers))
            raw_roster = _apply_founder_answers_to_content(raw_roster, review.answers)
            json_block = _extract_json_block(raw_roster)
            assert json_block is not None  # noqa: S101
            _write_roster(json_block, company)
            review = founder_review("Roster", roster_md_path, questions=_extract_founder_questions(raw_roster))
            continue

        if review.action == "modify" and review.feedback and review.feedback.strip().startswith("{"):
            # Founder is directly editing the roster JSON.
            edit_errors = _lint_roster(f"```json\n{review.feedback}\n```", standards_dir=standards_dir)
            if edit_errors:
                print("\n  Edited roster has validation errors:")
                for err in edit_errors:
                    print(f"    - {err}")
                print("  Please try again.\n")
                review = founder_review("Roster", roster_md_path)
                continue
            json_block = review.feedback
            assert json_block is not None  # noqa: S101
            raw_roster = f"```json\n{json_block}\n```"
            _write_roster(json_block, company)
            review = founder_review("Roster", roster_md_path, questions=_extract_founder_questions(raw_roster))
            continue

        rerun_context = context
        if review.action in {"modify", "request_more_questions"}:
            rerun_context = _build_revision_context(context, "current_roster", raw_roster)

        raw_roster = _agent_loop(
            hm,
            rerun_context,
            lambda c: _lint_roster(c, standards_dir=standards_dir),
            "Roster",
            founder_feedback=_review_feedback(review),
        )
        json_block = _extract_json_block(raw_roster)
        assert json_block is not None  # noqa: S101
        _write_roster(json_block, company)
        review = founder_review("Roster", roster_md_path, questions=_extract_founder_questions(raw_roster))


def _generate_single_role(
    entry: dict,
    company: Path,
    architecture_json: str,
    role_template: str,
    llm: LLMBackend,
) -> None:
    """Generate a single role file from a roster entry."""
    title = entry["title"]
    standards_dir = company / "standards"

    # Build assigned standards content.
    std_parts: list[str] = []
    for std_name in entry.get("assigned_standards", []):
        std_path = standards_dir / std_name
        if std_path.is_file():
            std_parts.append(std_path.read_text(encoding="utf-8"))
    standards_content = "\n\n---\n\n".join(std_parts) if std_parts else "(no standards assigned)"

    context = {
        "architecture": architecture_json,
        "role_title": title,
        "role_responsibility": entry["responsibility"],
        "role_template": role_template,
        "assigned_standards": standards_content,
    }

    rw = Agent(name="Role Writer", role_file=company / "roles" / "role_writer.md", llm=llm)
    role_content = _agent_loop(rw, context, _lint_role, f"Role: {title}")

    role_path = company / "roles" / entry["filename"]
    role_path.write_text(role_content, encoding="utf-8")
    print(f"   ✓ Generated role: {title} → {role_path}")


def _run_role_generation(company: Path, architecture_json: str, roster_json: str, llm: LLMBackend) -> None:
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

    print(f"\n>> Generating {total} role file(s)…")

    for idx, entry in enumerate(agents_list, 1):
        print(f"\n── Role {idx}/{total}: {entry['title']} ──")
        _generate_single_role(entry, company, architecture_json, role_template, llm)

    print(f"\n✓ Generated {total} role file(s)")


def _expected_role_paths(company: Path, roster_json: str) -> list[Path]:
    """Return the generated role file paths expected from *roster_json*."""
    try:
        data = json.loads(roster_json)
    except json.JSONDecodeError:
        logger.warning("Could not parse roster JSON while building expected role paths")
        return []

    agents = data.get("hired_agents")
    if not isinstance(agents, list):
        logger.warning("Roster JSON missing hired_agents while building expected role paths")
        return []

    paths: list[Path] = []
    for entry in agents:
        filename = entry.get("filename") if isinstance(entry, dict) else None
        if isinstance(filename, str) and filename:
            paths.append(company / "roles" / filename)

    return paths


def _run_or_skip_prd_phase(exec_ctx: PipelineExecutionContext) -> tuple[str, int | None]:
    """Return PRD content, running the phase only when needed."""
    prd_path = exec_ctx.company / "artifacts" / "prd.md"
    if _is_phase_done(exec_ctx.state, "prd", [prd_path]):
        print("\n↩ Skipping PRD phase (already completed)")
        return prd_path.read_text(encoding="utf-8"), None

    prd_content = _run_prd_phase(exec_ctx.company, exec_ctx.vision_content, exec_ctx.llm)
    mark_phase_complete(exec_ctx.workdir, exec_ctx.state, "prd")
    err = _try_commit(
        exec_ctx.workdir,
        "prd-generation",
        exec_ctx.options.no_commit,
        stage_all=exec_ctx.options.stage_all,
    )
    return prd_content, err


def _run_or_skip_architecture_phase(
    exec_ctx: PipelineExecutionContext,
    prd_content: str,
) -> tuple[str, int | None]:
    """Return architecture JSON, running the phase only when needed."""
    arch_json_path = exec_ctx.company / "artifacts" / "architecture.json"
    arch_md_path = exec_ctx.company / "artifacts" / "architecture.md"
    if _is_phase_done(exec_ctx.state, "architecture", [arch_json_path, arch_md_path]):
        print("\n↩ Skipping Architecture phase (already completed)")
        return arch_json_path.read_text(encoding="utf-8"), None

    arch_json = _run_architecture_phase(exec_ctx.company, exec_ctx.vision_content, prd_content, exec_ctx.llm)
    mark_phase_complete(exec_ctx.workdir, exec_ctx.state, "architecture")
    err = _try_commit(
        exec_ctx.workdir,
        "architecture-generation",
        exec_ctx.options.no_commit,
        stage_all=exec_ctx.options.stage_all,
    )
    return arch_json, err


def _run_or_skip_roster_phase(exec_ctx: PipelineExecutionContext, arch_json: str) -> str:
    """Return roster JSON, running the phase only when needed."""
    roster_json_path = exec_ctx.company / "artifacts" / "roster.json"
    roster_md_path = exec_ctx.company / "artifacts" / "roster.md"
    if _is_phase_done(exec_ctx.state, "roster", [roster_json_path, roster_md_path]):
        print("\n↩ Skipping Roster phase (already completed)")
        return roster_json_path.read_text(encoding="utf-8")

    roster_json = _run_roster_phase(exec_ctx.company, arch_json, exec_ctx.llm)
    mark_phase_complete(exec_ctx.workdir, exec_ctx.state, "roster")
    return roster_json


def _run_or_skip_role_generation_phase(
    exec_ctx: PipelineExecutionContext,
    arch_json: str,
    roster_json: str,
) -> int | None:
    """Run role generation when needed and validate expected files."""
    role_paths = _expected_role_paths(exec_ctx.company, roster_json)
    if role_paths and _is_phase_done(exec_ctx.state, "roles", role_paths):
        print("\n↩ Skipping Role Generation phase (already completed)")
        return None

    _run_role_generation(exec_ctx.company, arch_json, roster_json, exec_ctx.llm)
    role_paths = _expected_role_paths(exec_ctx.company, roster_json)
    if not role_paths or not all(path.is_file() for path in role_paths):
        print("\nError: role generation completed without writing all expected role files.", file=sys.stderr)
        return 1

    mark_phase_complete(exec_ctx.workdir, exec_ctx.state, "roles")
    return None


def _is_phase_done(state: dict | None, phase: str, artifact_paths: list[Path]) -> bool:
    """Check if *phase* completed previously and its artifacts still exist."""
    if state is None:
        return False
    completed = state.get("completed_phases", {})
    if phase not in completed:
        return False
    # All expected artifacts must be present on disk.
    return all(p.is_file() for p in artifact_paths)


def _prompt_vision_changed() -> str:
    """Ask the user what to do when the vision file has changed.

    Returns ``"continue"`` or ``"restart"``.
    """
    print("\n⚠  The vision file has changed since the last pipeline run.")
    print("   Previously completed phases may be based on stale content.")
    while True:
        choice = input("   [C]ontinue from where you left off, or [R]estart from scratch? ").strip().lower()
        if choice in ("c", "continue"):
            return "continue"
        if choice in ("r", "restart"):
            return "restart"
        print("   Please enter 'C' to continue or 'R' to restart.")


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


def _init_pipeline_state(workdir: Path, vision_path: Path) -> tuple[dict, Path]:
    """Load or create pipeline state, handling vision-change prompts.

    Returns ``(state, company)`` where *company* is the resolved
    ``.company/`` path (possibly rebuilt on restart).
    """
    company = init_company(workdir)
    print(f"\n✓ Company directory initialised: {company}")

    state = read_pipeline_state(workdir)
    vision_hash = hash_file(vision_path)

    if state is not None and state.get("vision_sha256") != vision_hash:
        action = _prompt_vision_changed()
        if action == "restart":
            clear_company(workdir)
            company = init_company(workdir)
            state = None
            print("✓ Restart: .company/ directory rebuilt.")

    if state is None:
        state = {"version": "0.2", "vision_sha256": vision_hash, "completed_phases": {}}
    else:
        state["vision_sha256"] = vision_hash
    write_pipeline_state(workdir, state)
    return state, company


def _run_phases(exec_ctx: PipelineExecutionContext) -> int:
    """Execute or skip each pipeline phase based on *state*.

    Returns 0 on success, non-zero on error.
    """
    prd_content, err = _run_or_skip_prd_phase(exec_ctx)
    if err is not None:
        return err

    arch_json, err = _run_or_skip_architecture_phase(exec_ctx, prd_content)
    if err is not None:
        return err

    roster_json = _run_or_skip_roster_phase(exec_ctx, arch_json)
    err = _run_or_skip_role_generation_phase(exec_ctx, arch_json, roster_json)
    if err is not None:
        return err

    err = _try_commit(
        exec_ctx.workdir,
        "hiring",
        exec_ctx.options.no_commit,
        stage_all=exec_ctx.options.stage_all,
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
    """Execute the full V0.2 SDLC pipeline.

    Returns 0 on success.
    """
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

    # 3. Load/create pipeline state (handles vision-change prompt).
    state, company = _init_pipeline_state(workdir, vision_path)

    # 4. Execute phases.
    exec_ctx = PipelineExecutionContext(
        state=state,
        company=company,
        vision_content=vision_content,
        llm=llm,
        options=options,
    )
    result = _run_phases(exec_ctx)
    if result != 0:
        return result

    # ── Done ─────────────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("  Pipeline complete.")
    print("=" * 72)
    return 0
