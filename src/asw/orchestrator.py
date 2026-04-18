"""Pipeline orchestrator – the main SDLC loop."""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

from asw.agents.base import Agent
from asw.company import init_company
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


def _render_architecture_markdown(json_str: str, mermaid_str: str) -> str:
    """Render a human-readable Markdown from architecture JSON and Mermaid."""
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        # Fallback if JSON is somehow invalid after linting (unlikely)
        return (
            "# System Architecture\n\n"
            "> **Warning:** Technical specification could not be parsed.\n\n"
            f"```mermaid\n{mermaid_str}\n```"
        )

    lines = ["# System Architecture", ""]
    lines.append(
        "> **Source of Truth:** The technical specification for this architecture is stored in `architecture.json`."
    )
    lines.append("")
    lines.append("## Visual Overview")
    lines.append(f"```mermaid\n{mermaid_str}\n```")
    lines.append("")

    # Project Info
    name = data.get("project_name", "N/A")
    lines.append(f"## Project: {name}")
    lines.append("")

    # Tech Stack
    ts = data.get("tech_stack", {})
    lines.append("### Tech Stack")
    lines.append(f"- **Language:** {ts.get('language', 'N/A')} ({ts.get('version', 'N/A')})")
    frameworks = _safe_join(ts.get("frameworks", [])) or "None"
    lines.append(f"- **Frameworks:** {frameworks}")
    tools = _safe_join(ts.get("tools", [])) or "None"
    lines.append(f"- **Tools:** {tools}")
    lines.append("")

    # Components
    lines.append("### Components")
    lines.append("| Name | Responsibility | Interfaces |")
    lines.append("| --- | --- | --- |")
    for comp in data.get("components", []):
        name = comp.get("name", "N/A")
        resp = comp.get("responsibility", "N/A")
        iface = _safe_join(comp.get("interfaces", [])) or "None"
        lines.append(f"| {name} | {resp} | {iface} |")
    lines.append("")

    # Data Models
    lines.append("### Data Models")
    for model in data.get("data_models", []):
        mname = model.get("name", "N/A")
        lines.append(f"#### {mname}")
        lines.append("| Field | Type |")
        lines.append("| --- | --- |")
        for field in model.get("fields", []):
            fname = field.get("name", "N/A")
            ftype = field.get("type", "N/A")
            lines.append(f"| {fname} | {ftype} |")
        lines.append("")

    # API Contracts
    lines.append("### API Contracts")
    lines.append("| Endpoint | Method | Description |")
    lines.append("| --- | --- | --- |")
    for api in data.get("api_contracts", []):
        ep = api.get("endpoint", "N/A")
        meth = api.get("method", "N/A")
        desc = api.get("description", "N/A")
        lines.append(f"| {ep} | {meth} | {desc} |")
    lines.append("")

    # Deployment
    dep = data.get("deployment", {})
    lines.append("### Deployment")
    lines.append(f"- **Platform:** {dep.get('platform', 'N/A')}")
    lines.append(f"- **Strategy:** {dep.get('strategy', 'N/A')}")
    reqs = _safe_join(dep.get("requirements", [])) or "None"
    lines.append(f"- **Requirements:** {reqs}")
    lines.append("")

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
    lint_fn: object,  # callable
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

        errors = lint_fn(output)  # type: ignore[operator]
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


def run_pipeline(
    *, vision_path: Path, workdir: Path, no_commit: bool = False, debug: bool = False
) -> int:  # pylint: disable=too-many-locals
    """Execute the full V0.1 SDLC pipeline.

    Returns 0 on success.
    """
    logger.debug(
        "run_pipeline called: vision_path=%s workdir=%s no_commit=%s debug=%s", vision_path, workdir, no_commit, debug
    )
    print("=" * 72)
    print("  AgenticOrg CLI – V0.1 Pipeline")
    print("=" * 72)

    # 0. Validate git repo early (unless commits are disabled).
    if not no_commit and not is_git_repo(workdir):
        print(f"\nError: {workdir} is not inside a git repository.", file=sys.stderr)
        print("  Run: git init && git commit --allow-empty -m 'Initial commit'", file=sys.stderr)
        print("  Or skip git entirely: asw start --vision <file> --no-commit", file=sys.stderr)
        return 1

    # 1. Initialise .company/ directory.
    company = init_company(workdir)
    print(f"\n✓ Company directory initialised: {company}")

    # 2. Read vision.
    vision_content = vision_path.read_text(encoding="utf-8")
    logger.debug("Vision content (%d chars):\n%s", len(vision_content), vision_content)
    print(f"✓ Vision loaded: {vision_path.name} ({len(vision_content)} chars)")

    # 3. Get LLM backend.
    llm: LLMBackend = get_backend("gemini")
    logger.debug("LLM backend acquired: gemini")
    print("✓ LLM backend: Gemini CLI")

    # ── Phase A: CPO → PRD ───────────────────────────────────────────────
    prd_content = _run_prd_phase(company, vision_content, llm)

    if not no_commit:
        try:
            commit_state(workdir, "prd-generation")
        except GitError as exc:
            print(f"\nError committing prd-generation: {exc}", file=sys.stderr)
            return 1
    else:
        print("  (skipping git commit – --no-commit)")

    # ── Phase B: CTO → Architecture ──────────────────────────────────────
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

    if not no_commit:
        try:
            commit_state(workdir, "architecture-generation")
        except GitError as exc:
            print(f"\nError committing architecture-generation: {exc}", file=sys.stderr)
            return 1
    else:
        print("  (skipping git commit – --no-commit)")

    # ── Done ─────────────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("  Pipeline complete.")
    print("=" * 72)
    return 0
