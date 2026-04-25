"""Git state machine – auto-commit at phase boundaries."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from subprocess import run as _subprocess_run

logger = logging.getLogger("asw.git")


class GitError(RuntimeError):
    """Raised when a git operation fails."""


def _run_git(workdir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command inside *workdir* and return the result."""
    logger.debug("Running: git %s (cwd=%s)", " ".join(args), workdir)
    result = _subprocess_run(
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


def repo_root(workdir: Path) -> Path:
    """Return the top-level git repository path for *workdir*."""
    result = _run_git(workdir, "rev-parse", "--show-toplevel")
    return Path(result.stdout.strip())


def worktree_changed_paths(workdir: Path) -> list[str]:
    """Return repo-relative changed paths from the current worktree."""
    root = repo_root(workdir)
    result = _run_git(root, "status", "--short", "--untracked-files=all")

    paths: list[str] = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        raw_path = line[3:].strip()
        if not raw_path:
            continue
        if " -> " in raw_path:
            old_path, new_path = raw_path.split(" -> ", maxsplit=1)
            paths.extend([old_path.strip(), new_path.strip()])
            continue
        paths.append(raw_path)

    return sorted(dict.fromkeys(path for path in paths if path))


def commit_state(
    workdir: Path,
    phase_name: str,
    *,
    stage_all: bool = False,
    approved_paths: list[str] | None = None,
) -> str:
    """Stage ``.company/`` or the full repo and create a commit.

    Returns the commit hash.
    """
    if not is_git_repo(workdir):
        msg = f"Not a git repository: {workdir}"
        raise GitError(msg)

    if stage_all and approved_paths is not None:
        msg = "approved_paths cannot be combined with stage_all=True"
        raise ValueError(msg)

    if stage_all:
        root = repo_root(workdir)
        _run_git(root, "add", "--all")
    elif approved_paths is not None:
        root = repo_root(workdir)
        company_rel_path = _repo_relative_path(root, workdir / ".company")
        stage_paths = [company_rel_path, *approved_paths]
        _run_git(root, "add", "--", *stage_paths)
    else:
        _run_git(workdir, "add", ".company/")

    message = f"[asw] Phase: {phase_name} completed"

    # Check whether there are staged changes before committing.
    diff_result = _subprocess_run(
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


def _repo_relative_path(root: Path, path: Path) -> str:
    """Return *path* relative to the repository root."""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise GitError(f"Path is outside the repository root: {path}") from exc
