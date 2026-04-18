"""AgenticOrg CLI entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="asw",
        description=("AgenticOrg CLI – orchestrate a simulated company" " of LLM-based software development agents."),
        epilog="Tip: use 'asw start --no-commit' to run without requiring a git repository.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    start_parser = subparsers.add_parser(
        "start",
        help="Start the agentic SDLC pipeline from a vision document.",
    )
    start_parser.add_argument(
        "--vision",
        type=Path,
        required=True,
        help="Path to the vision Markdown file.",
    )
    start_parser.add_argument(
        "--workdir",
        type=Path,
        default=Path.cwd(),
        help="Working directory for .company/ state (default: current directory).",
    )
    start_parser.add_argument(
        "--no-commit",
        action="store_true",
        default=False,
        help="Skip git commits at phase boundaries (useful for testing or drafts).",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI main entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    vision_path: Path = args.vision.resolve()
    workdir: Path = args.workdir.resolve()

    if not vision_path.is_file():
        print(f"Error: vision file not found: {vision_path}", file=sys.stderr)
        return 1

    if not workdir.is_dir():
        print(f"Error: working directory does not exist: {workdir}", file=sys.stderr)
        return 1

    # Lazy import to keep CLI startup fast.
    from asw.orchestrator import run_pipeline  # noqa: PLC0415

    return run_pipeline(vision_path=vision_path, workdir=workdir, no_commit=args.no_commit)


if __name__ == "__main__":
    raise SystemExit(main())
