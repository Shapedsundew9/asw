"""AgenticOrg CLI entry point."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


class LoggingConfigError(RuntimeError):
    """Raised when debug logging cannot be configured."""


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="asw",
        description=("AgenticOrg CLI – orchestrate a simulated company" " of LLM-based software development agents."),
        epilog=(
            "Use 'asw <command> --help' for command-specific options. "
            "Example: 'asw start --help'. "
            "Tip: use 'asw start --no-commit' to run without requiring a git repository."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    start_parser = subparsers.add_parser(
        "start",
        description="Start the agentic SDLC pipeline from a vision document.",
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
    start_parser.add_argument(
        "--stage-all",
        action="store_true",
        default=False,
        help="Stage the full git worktree during phase commits (default: .company/ only).",
    )
    start_parser.add_argument(
        "--restart",
        action="store_true",
        default=False,
        help="Delete existing .company/ directory and restart the pipeline from scratch.",
    )
    start_parser.add_argument(
        "--execute-phase-setups",
        action="store_true",
        default=False,
        help="Allow Founder-approved phase setup scripts to execute inside the workspace.",
    )
    start_parser.add_argument(
        "--debug",
        nargs="?",
        const=True,
        default=None,
        metavar="LOGFILE",
        help=(
            "Enable debug logging to a file. "
            "Optionally specify a log file path; if omitted, a timestamped "
            "file is created in the current directory."
        ),
    )

    return parser


def _resolve_log_path(debug: bool | str) -> Path:
    """Resolve and validate the debug log path."""
    if isinstance(debug, str):
        log_path = Path(debug).expanduser().resolve()
    else:
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
        log_path = Path.cwd() / f"asw-debug-{stamp}.log"

    if log_path.exists() and log_path.is_dir():
        msg = f"debug log path is a directory: {log_path}"
        raise LoggingConfigError(msg)

    parent = log_path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        msg = f"could not create debug log directory: {parent}\n  {exc}"
        raise LoggingConfigError(msg) from exc

    if not os.access(parent, os.W_OK):
        msg = f"debug log directory is not writable: {parent}"
        raise LoggingConfigError(msg)

    return log_path


def _configure_logging(debug: bool | str | None) -> None:
    """Set up the ``asw`` logger hierarchy.

    When *debug* is ``None`` logging is effectively silent (WARNING level,
    no file handler).  When *debug* is ``True`` a timestamped log file is
    created in the current directory.  When *debug* is a string it is used
    as the log file path.
    """
    root = logging.getLogger("asw")

    if debug is None:
        root.setLevel(logging.WARNING)
        return

    root.setLevel(logging.DEBUG)

    log_path = _resolve_log_path(debug)

    try:
        handler = logging.FileHandler(log_path, encoding="utf-8")
    except OSError as exc:
        msg = f"could not open debug log file: {log_path}\n  {exc}"
        raise LoggingConfigError(msg) from exc

    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s"))
    root.addHandler(handler)

    print(f"Debug log: {log_path}")


def main(argv: list[str] | None = None) -> int:
    """CLI main entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        _configure_logging(args.debug)
    except LoggingConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    vision_path: Path = args.vision.resolve()
    workdir: Path = args.workdir.resolve()

    if not vision_path.is_file():
        print(f"Error: vision file not found: {vision_path}", file=sys.stderr)
        return 1

    if not workdir.is_dir():
        print(f"Error: working directory does not exist: {workdir}", file=sys.stderr)
        return 1

    # Lazy import to keep CLI startup fast.
    from asw.orchestrator import PipelineRunOptions, run_pipeline

    options = PipelineRunOptions(
        no_commit=args.no_commit,
        stage_all=args.stage_all,
        debug=bool(args.debug),
        restart=args.restart,
        execute_phase_setups=args.execute_phase_setups,
    )

    return run_pipeline(
        vision_path=vision_path,
        workdir=workdir,
        options=options,
    )


if __name__ == "__main__":
    raise SystemExit(main())
