"""Pipeline orchestrator – the main SDLC loop."""

from __future__ import annotations

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
        print(f"\n>> {agent.name} – attempt {attempt}")
        output = agent.run(context, feedback=feedback)

        errors = lint_fn(output)  # type: ignore[operator]
        if not errors:
            print(f"   Lint passed for {phase_name}.")
            return output

        print(f"   Lint FAILED ({len(errors)} error(s)):")
        for err in errors:
            print(f"     - {err}")

        if attempt > _MAX_RETRIES:
            print(f"\nFATAL: {agent.name} failed to produce valid output after {_MAX_RETRIES + 1} attempts.")
            sys.exit(1)

        feedback = "The previous output failed mechanical validation." " Fix these errors:\n" + "\n".join(
            f"- {e}" for e in errors
        )

    # Unreachable but keeps mypy happy.
    msg = "Unreachable"
    raise AssertionError(msg)


def _write_architecture(raw_arch: str, company: Path) -> None:
    """Parse CTO output and write architecture artifacts."""
    _, json_str, mermaid_str = _lint_architecture(raw_arch)

    arch_json_path = company / "artifacts" / "architecture.json"
    arch_json_path.write_text(json_str or "", encoding="utf-8")

    arch_md_path = company / "artifacts" / "architecture.md"
    arch_md_content = f"# System Architecture Diagram\n\n" f"```mermaid\n{mermaid_str or ''}\n```\n"
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


def run_pipeline(*, vision_path: Path, workdir: Path, no_commit: bool = False) -> int:
    """Execute the full V0.1 SDLC pipeline.

    Returns 0 on success.
    """
    print("=" * 72)
    print("  AgenticOrg CLI – V0.1 Pipeline")
    print("=" * 72)

    # 0. Validate git repo early (unless commits are disabled).
    if not no_commit and not is_git_repo(workdir):
        print(f"\nError: {workdir} is not inside a git repository.", file=sys.stderr)
        print("Initialise a git repo first, or use --no-commit.", file=sys.stderr)
        return 1

    # 1. Initialise .company/ directory.
    company = init_company(workdir)
    print(f"\n✓ Company directory initialised: {company}")

    # 2. Read vision.
    vision_content = vision_path.read_text(encoding="utf-8")
    print(f"✓ Vision loaded: {vision_path.name} ({len(vision_content)} chars)")

    # 3. Get LLM backend.
    llm: LLMBackend = get_backend("gemini")
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

    arch_json_path = company / "artifacts" / "architecture.json"
    choice, feedback = founder_review("Architecture", arch_json_path)
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
        choice, feedback = founder_review("Architecture", arch_json_path)

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
