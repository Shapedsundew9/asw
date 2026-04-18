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
    write_pipeline_state,
)
from asw.gates import founder_review
from asw.git import GitError, commit_state, is_git_repo
from asw.linters.json_lint import validate_architecture
from asw.linters.markdown import validate_checklist, validate_mermaid, validate_sections
from asw.llm.backend import LLMBackend, get_backend

_MAX_RETRIES = 2

logger = logging.getLogger("asw.orchestrator")


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
    lines = ["### Components", "| Name | Responsibility | Interfaces |", "| --- | --- | --- |"]
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
    lines = ["### API Contracts", "| Endpoint | Method | Description |", "| --- | --- | --- |"]
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
    match = re.search(r"```json\s*\n(.*?)```", content, re.DOTALL)
    return match.group(1).strip() if match else None


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
    """Run an agent in a retry loop until lint passes or retries are exhausted."""
    feedback: str | None = founder_feedback

    for attempt in range(1, _MAX_RETRIES + 2):  # 1 initial + _MAX_RETRIES
        logger.debug("Agent loop: %s attempt %d/%d", agent.name, attempt, _MAX_RETRIES + 1)
        print(f"\n>> {agent.name} – attempt {attempt}")
        print(f"   Invoking {agent.name} via Gemini CLI (may take up to 5 min)…", flush=True)
        output = agent.run(context, feedback=feedback)
        logger.debug("Agent %s raw output (%d chars):\n%s", agent.name, len(output), output)
        print("   Response received.")

        errors = lint_fn(output)
        if not errors:
            print(f"   Lint passed for {phase_name}.")
            return output

        print(f"   Lint FAILED ({len(errors)} error(s)):")
        for err in errors:
            print(f"     - {err}")

        if attempt > _MAX_RETRIES:
            logger.debug("Agent %s exhausted retries – exiting", agent.name)
            print(f"\nFATAL: {agent.name} failed to produce valid output after {_MAX_RETRIES + 1} attempts.")
            print("  → Try simplifying your vision document and re-running `asw start`.")
            sys.exit(1)

        feedback = "The previous output failed mechanical validation." " Fix these errors:\n" + "\n".join(
            f"- {e}" for e in errors
        )
        logger.debug("Feedback for next attempt:\n%s", feedback)

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
    prd_content = _agent_loop(cpo, {"vision": vision_content}, _lint_prd, "PRD")

    prd_path = company / "artifacts" / "prd.md"
    prd_path.write_text(prd_content, encoding="utf-8")
    print(f"\n✓ PRD written: {prd_path}")

    choice, feedback = founder_review("PRD", prd_path)
    while choice in ("r", "m"):
        founder_feedback = feedback if choice == "m" else None
        prd_content = _agent_loop(cpo, {"vision": vision_content}, _lint_prd, "PRD", founder_feedback=founder_feedback)
        prd_path.write_text(prd_content, encoding="utf-8")
        choice, feedback = founder_review("PRD", prd_path)
    return prd_content


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
    choice, feedback = founder_review("Architecture", arch_md_path)
    while choice in ("r", "m"):
        founder_feedback = feedback if choice == "m" else None
        raw_arch = _agent_loop(
            cto,
            arch_context,
            lambda c: _lint_architecture(c)[0],
            "Architecture",
            founder_feedback=founder_feedback,
        )
        _write_architecture(raw_arch, company)
        choice, feedback = founder_review("Architecture", arch_md_path)

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
    hm = Agent(name="Hiring Manager", role_file=company / "roles" / "hiring_manager.md", llm=llm)
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
    choice, feedback = founder_review("Roster", roster_md_path)
    while choice in ("r", "m"):
        if choice == "m" and feedback:
            # Founder is directly editing the roster JSON.
            edit_errors = _lint_roster(f"```json\n{feedback}\n```", standards_dir=standards_dir)
            if edit_errors:
                print("\n  Edited roster has validation errors:")
                for err in edit_errors:
                    print(f"    - {err}")
                print("  Please try again.\n")
                choice, feedback = founder_review("Roster", roster_md_path)
                continue
            json_block = feedback
            _write_roster(json_block, company)
            choice, feedback = founder_review("Roster", roster_md_path)
        else:
            # Reject: re-run from scratch.
            raw_roster = _agent_loop(
                hm,
                context,
                lambda c: _lint_roster(c, standards_dir=standards_dir),
                "Roster",
            )
            json_block = _extract_json_block(raw_roster)
            assert json_block is not None  # noqa: S101
            _write_roster(json_block, company)
            choice, feedback = founder_review("Roster", roster_md_path)

    return json_block


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

    total = len(agents_list)

    print(f"\n>> Generating {total} role file(s)…")

    for idx, entry in enumerate(agents_list, 1):
        print(f"\n── Role {idx}/{total}: {entry['title']} ──")
        _generate_single_role(entry, company, architecture_json, role_template, llm)

    print(f"\n✓ Generated {total} role file(s)")


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


def _try_commit(workdir: Path, phase_name: str, no_commit: bool) -> int | None:
    """Attempt a git commit.  Returns an error code on failure, else *None*."""
    if no_commit:
        print("  (skipping git commit – --no-commit)")
        return None
    try:
        commit_state(workdir, phase_name)
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


def _run_phases(
    state: dict,
    company: Path,
    vision_content: str,
    llm: LLMBackend,
    *,
    no_commit: bool,
) -> int:
    """Execute or skip each pipeline phase based on *state*.

    Returns 0 on success, non-zero on error.
    """
    workdir = company.parent
    # ── Phase A: CPO → PRD ───────────────────────────────────────────────
    prd_path = company / "artifacts" / "prd.md"
    if _is_phase_done(state, "prd", [prd_path]):
        prd_content = prd_path.read_text(encoding="utf-8")
        print("\n↩ Skipping PRD phase (already completed)")
    else:
        prd_content = _run_prd_phase(company, vision_content, llm)
        mark_phase_complete(workdir, state, "prd")
        err = _try_commit(workdir, "prd-generation", no_commit)
        if err is not None:
            return err

    # ── Phase B: CTO → Architecture ──────────────────────────────────────
    arch_json_path = company / "artifacts" / "architecture.json"
    arch_md_path = company / "artifacts" / "architecture.md"
    if _is_phase_done(state, "architecture", [arch_json_path, arch_md_path]):
        arch_json = arch_json_path.read_text(encoding="utf-8")
        print("\n↩ Skipping Architecture phase (already completed)")
    else:
        arch_json = _run_architecture_phase(company, vision_content, prd_content, llm)
        mark_phase_complete(workdir, state, "architecture")
        err = _try_commit(workdir, "architecture-generation", no_commit)
        if err is not None:
            return err

    # ── Phase C1: Hiring Manager → Roster ────────────────────────────────
    roster_json_path = company / "artifacts" / "roster.json"
    roster_md_path = company / "artifacts" / "roster.md"
    if _is_phase_done(state, "roster", [roster_json_path, roster_md_path]):
        roster_json = roster_json_path.read_text(encoding="utf-8")
        print("\n↩ Skipping Roster phase (already completed)")
    else:
        roster_json = _run_roster_phase(company, arch_json, llm)
        mark_phase_complete(workdir, state, "roster")

    # ── Phase C2: Role Writer → Role files ───────────────────────────────
    if _is_phase_done(state, "roles", []):
        print("\n↩ Skipping Role Generation phase (already completed)")
    else:
        _run_role_generation(company, arch_json, roster_json, llm)
        mark_phase_complete(workdir, state, "roles")

    # Commit hiring phases together.
    err = _try_commit(workdir, "hiring", no_commit)
    if err is not None:
        return err

    return 0


def run_pipeline(
    *,
    vision_path: Path,
    workdir: Path,
    no_commit: bool = False,
    debug: bool = False,
    restart: bool = False,
) -> int:
    """Execute the full V0.2 SDLC pipeline.

    Returns 0 on success.
    """
    logger.debug(
        "run_pipeline called: vision_path=%s workdir=%s no_commit=%s debug=%s restart=%s",
        vision_path,
        workdir,
        no_commit,
        debug,
        restart,
    )
    print("=" * 72)
    print("  AgenticOrg CLI – V0.2 Pipeline")
    print("=" * 72)

    # 0. Validate git repo early (unless commits are disabled).
    if not no_commit and not is_git_repo(workdir):
        print(f"\nError: {workdir} is not inside a git repository.", file=sys.stderr)
        print("  Run: git init && git commit --allow-empty -m 'Initial commit'", file=sys.stderr)
        print("  Or skip git entirely: asw start --vision <file> --no-commit", file=sys.stderr)
        return 1

    # 0b. Handle --restart: wipe .company/ before anything else.
    if restart:
        clear_company(workdir)
        print("\n✓ Restart: .company/ directory removed.")

    # 1. Read vision.
    vision_content = vision_path.read_text(encoding="utf-8")
    logger.debug("Vision content (%d chars):\n%s", len(vision_content), vision_content)
    print(f"✓ Vision loaded: {vision_path.name} ({len(vision_content)} chars)")

    # 2. Get LLM backend.
    llm: LLMBackend = get_backend("gemini")
    logger.debug("LLM backend acquired: gemini")
    print("✓ LLM backend: Gemini CLI")

    # 3. Load/create pipeline state (handles vision-change prompt).
    state, company = _init_pipeline_state(workdir, vision_path)

    # 4. Execute phases.
    result = _run_phases(state, company, vision_content, llm, no_commit=no_commit)
    if result != 0:
        return result

    # ── Done ─────────────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("  Pipeline complete.")
    print("=" * 72)
    return 0
