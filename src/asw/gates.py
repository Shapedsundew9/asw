"""Founder Review Gate – terminal-based approval workflow."""

from __future__ import annotations

import sys
from pathlib import Path

_VALID_CHOICES = {"a", "r", "m", "s"}


def founder_review(phase_name: str, artifact_path: Path) -> tuple[str, str | None]:
    """Pause for Founder review of an artifact.

    Displays a summary and prompts for a decision.

    Parameters
    ----------
    phase_name:
        Human-readable name of the current phase (e.g. ``"PRD"``).
    artifact_path:
        Path to the artifact file to review.

    Returns:
    -------
    tuple[str, str | None]
        ``(choice, feedback)`` where *choice* is one of
        ``"a"`` (approve), ``"r"`` (reject), ``"m"`` (modify), ``"s"`` (stop)
        and *feedback* is the Founder's text when *choice* is ``"m"``, else ``None``.
    """
    _preview_cap = 6000
    content = artifact_path.read_text(encoding="utf-8")
    truncated = len(content) > _preview_cap
    preview = content[:_preview_cap]

    print("\n" + "=" * 72)
    print(f"  FOUNDER REVIEW GATE  –  Phase: {phase_name}")
    print("=" * 72)
    print(f"\nArtifact: {artifact_path}\n")
    print(preview)
    if truncated:
        print(f"\n... ({len(content) - _preview_cap} more characters — to read in full: less {artifact_path})")
    print("\n" + "-" * 72)

    while True:
        raw = input("[A]pprove  [R]eject  [M]odify  [S]top  > ").strip().lower()
        if raw and raw[0] in _VALID_CHOICES:
            choice = raw[0]
            break
        print("Invalid choice. Please enter A, R, M, or S.")

    feedback: str | None = None
    if choice == "m":
        print("Enter your feedback below.")
        print("Type each line and press Enter. Press Enter on a BLANK LINE to submit.")
        print("-" * 72)
        lines: list[str] = []
        while True:
            line = input()
            if not line:
                break
            lines.append(line)
        print(f"──── Feedback captured ({len(lines)} line(s)) ────")
        feedback = "\n".join(lines)

    if choice == "s":
        print("\nPipeline stopped by Founder.")
        sys.exit(0)

    return choice, feedback
