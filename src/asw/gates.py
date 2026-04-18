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
    content = artifact_path.read_text(encoding="utf-8")
    preview = content[:2000]
    if len(content) > 2000:
        preview += f"\n\n... ({len(content) - 2000} more characters)"

    print("\n" + "=" * 72)
    print(f"  FOUNDER REVIEW GATE  –  Phase: {phase_name}")
    print("=" * 72)
    print(f"\nArtifact: {artifact_path}\n")
    print(preview)
    print("\n" + "-" * 72)

    while True:
        raw = input("[A]pprove  [R]eject  [M]odify  [S]top  > ").strip().lower()
        if raw and raw[0] in _VALID_CHOICES:
            choice = raw[0]
            break
        print("Invalid choice. Please enter A, R, M, or S.")

    feedback: str | None = None
    if choice == "m":
        print("Enter your modification feedback (end with an empty line):")
        lines: list[str] = []
        while True:
            line = input()
            if not line:
                break
            lines.append(line)
        feedback = "\n".join(lines)

    if choice == "s":
        print("\nPipeline stopped by Founder.")
        sys.exit(0)

    return choice, feedback
