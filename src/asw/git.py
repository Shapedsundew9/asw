"""Git state machine – auto-commit at phase boundaries."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger("asw.git")


class GitError(RuntimeError):
    """Raised when a git operation fails."""


def _run_git(workdir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command inside *workdir* and return the result."""
    logger.debug("Running: git %s (cwd=%s)", " ".join(args), workdir)
    result = subprocess.run(  # noqa: S603, S607
        ["git", *args],
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        msg = f"git {' '.join(args)} failed:\n{result.stderr.strip()}"
        raise GitError(msg)
    return result


def is_git_repo(workdir: Path) -> bool:
    """Return ``True`` if *workdir* is inside a git repository."""
    try:
        _run_git(workdir, "rev-parse", "--is-inside-work-tree")
    except GitError:
        return False
    return True


def commit_state(workdir: Path, phase_name: str) -> str:
    """Stage ``.company/`` and ``src/`` and create a commit.

    Returns the commit hash.
    """
    if not is_git_repo(workdir):
        msg = f"Not a git repository: {workdir}"
        raise GitError(msg)

    _run_git(workdir, "add", ".company/")
    if (workdir / "src").is_dir():
        _run_git(workdir, "add", "src/")

    message = f"[asw] Phase: {phase_name} completed"

    # Check whether there are staged changes before committing.
    diff_result = subprocess.run(  # noqa: S603, S607
        ["git", "diff", "--cached", "--quiet"],
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
    )
    if diff_result.returncode == 0:
        # No staged changes – nothing to commit.
        print(f"  (no changes to commit for phase '{phase_name}')")
        return ""

    _run_git(workdir, "commit", "-m", message)

    result = _run_git(workdir, "rev-parse", "HEAD")
    commit_hash = result.stdout.strip()
    print(f"  Committed: {commit_hash[:8]} – {message}")
    return commit_hash
