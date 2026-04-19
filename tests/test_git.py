"""Tests for git staging scope during phase commits."""

from __future__ import annotations

import subprocess
from pathlib import Path

from asw.company import init_company
from asw.git import commit_state


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
