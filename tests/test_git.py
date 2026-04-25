"""Tests for git staging scope during phase commits."""

from __future__ import annotations

import subprocess
from pathlib import Path

from asw.company import init_company
from asw.git import commit_state, worktree_changed_paths


def _setup_git_repo(path: Path) -> None:
    """Initialise a minimal git repo for commit-scope tests."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@test.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True, capture_output=True)


def _git_output(repo: Path, *args: str) -> str:
    """Run git and return stdout as text."""
    result = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)
    return result.stdout


def test_commit_state_stages_company_only_by_default_from_subdir(tmp_path: Path) -> None:
    """Default commits should only include .company/ changes."""
    _setup_git_repo(tmp_path)
    workdir = tmp_path / "app"
    workdir.mkdir()
    company = init_company(workdir)
    (company / "artifacts" / "note.txt").write_text("company change", encoding="utf-8")
    (tmp_path / "README.md").write_text("root change", encoding="utf-8")

    commit_state(workdir, "test-phase")

    committed_files = _git_output(tmp_path, "show", "--name-only", "--pretty=format:", "HEAD")
    status = _git_output(tmp_path, "status", "--short")

    assert "app/.company/artifacts/note.txt" in committed_files
    assert "README.md" not in committed_files
    assert "README.md" in status


def test_commit_state_stage_all_stages_full_repo_from_subdir(tmp_path: Path) -> None:
    """Repo-wide staging should include files outside the working directory."""
    _setup_git_repo(tmp_path)
    workdir = tmp_path / "app"
    workdir.mkdir()
    company = init_company(workdir)
    (company / "artifacts" / "note.txt").write_text("company change", encoding="utf-8")
    (tmp_path / "README.md").write_text("root change", encoding="utf-8")

    commit_state(workdir, "test-phase", stage_all=True)

    committed_files = _git_output(tmp_path, "show", "--name-only", "--pretty=format:", "HEAD")
    status = _git_output(tmp_path, "status", "--short")

    assert "app/.company/artifacts/note.txt" in committed_files
    assert "README.md" in committed_files
    assert status.strip() == ""


def test_worktree_changed_paths_reports_repo_relative_paths(tmp_path: Path) -> None:
    """Changed-path collection should include tracked and untracked repo-relative paths."""
    _setup_git_repo(tmp_path)
    workdir = tmp_path / "app"
    workdir.mkdir()
    company = init_company(workdir)
    tracked = tmp_path / "README.md"
    tracked.write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "base"], check=True, capture_output=True)

    tracked.write_text("updated\n", encoding="utf-8")
    (company / "artifacts" / "note.txt").write_text("company change", encoding="utf-8")
    (workdir / "new_module.py").write_text("print('hi')\n", encoding="utf-8")

    changed = worktree_changed_paths(workdir)

    assert "README.md" in changed
    assert "app/.company/artifacts/note.txt" in changed
    assert "app/new_module.py" in changed


def test_commit_state_stages_only_approved_paths_and_company_artifacts(tmp_path: Path) -> None:
    """Approved-path commits should stage only the selected repo files plus .company artifacts."""
    _setup_git_repo(tmp_path)
    workdir = tmp_path / "app"
    workdir.mkdir()
    company = init_company(workdir)
    approved = tmp_path / "README.md"
    approved.write_text("approved\n", encoding="utf-8")
    other = tmp_path / "docs.md"
    other.write_text("other\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "README.md", "docs.md", "app/.company"],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "base"], check=True, capture_output=True)

    approved.write_text("approved change\n", encoding="utf-8")
    other.write_text("other change\n", encoding="utf-8")
    (company / "artifacts" / "note.txt").write_text("company change", encoding="utf-8")

    commit_state(workdir, "test-phase", approved_paths=["README.md"])

    committed_files = _git_output(tmp_path, "show", "--name-only", "--pretty=format:", "HEAD")
    status = _git_output(tmp_path, "status", "--short")

    assert "README.md" in committed_files
    assert "docs.md" not in committed_files
    assert "app/.company/artifacts/note.txt" in committed_files
    assert "docs.md" in status
