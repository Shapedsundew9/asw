"""Tests for the isolated test-company runner."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from asw.test_company_runner import (
    IsolatedTestCompanyConfig,
    IsolatedTestCompanyError,
    IsolatedTestCompanyRunner,
    build_devcontainer_exec_command,
    build_start_command,
    collect_required_env,
    copy_workspace_snapshot,
)


def test_collect_required_env_rejects_missing_gemini_key() -> None:
    """The isolated runner should fail before launching without credentials."""
    with pytest.raises(IsolatedTestCompanyError) as excinfo:
        collect_required_env({})

    assert "GEMINI_API_KEY" in str(excinfo.value)


def test_copy_workspace_snapshot_excludes_runtime_and_cache_paths(tmp_path: Path) -> None:
    """Snapshot copies should exclude live runtime state and cache directories."""
    source = tmp_path / "source"
    source.mkdir()
    (source / ".devcontainer").mkdir()
    (source / ".devcontainer" / "devcontainer.json").write_text("{}", encoding="utf-8")
    (source / "src").mkdir()
    (source / "src" / "feature.py").write_text("print('ok')\n", encoding="utf-8")
    (source / "tests").mkdir()
    (source / "tests" / "test_vision.md").write_text("# Vision\n", encoding="utf-8")
    (source / "tests" / "test_company").mkdir()
    (source / "tests" / "test_company" / "debug.log").write_text("old", encoding="utf-8")
    (source / ".git").mkdir()
    (source / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (source / ".venv").mkdir()
    (source / ".venv" / "marker").write_text("venv", encoding="utf-8")
    (source / ".company").mkdir()
    (source / ".company" / "state.json").write_text("{}", encoding="utf-8")
    (source / "pkg").mkdir()
    (source / "pkg" / "__pycache__").mkdir()
    (source / "pkg" / "__pycache__" / "module.pyc").write_bytes(b"pyc")

    destination = tmp_path / "destination"
    copy_workspace_snapshot(source, destination)

    assert (destination / ".devcontainer" / "devcontainer.json").is_file()
    assert (destination / "src" / "feature.py").is_file()
    assert (destination / "tests" / "test_vision.md").is_file()
    assert (destination / "tests" / "test_company").is_dir()
    assert not (destination / "tests" / "test_company" / "debug.log").exists()
    assert not (destination / ".git").exists()
    assert not (destination / ".venv").exists()
    assert not (destination / ".company").exists()
    assert not (destination / "pkg" / "__pycache__").exists()


def test_build_devcontainer_exec_command_wraps_start_command() -> None:
    """The devcontainer exec command should run the safe-path start command via bash."""
    config = IsolatedTestCompanyConfig(repo_root=Path("/repo"))

    command = build_devcontainer_exec_command(Path("/tmp/workspace"), build_start_command(config))

    assert command[:4] == ["devcontainer", "exec", "--workspace-folder", "/tmp/workspace"]
    assert command[4:6] == ["bash", "-lc"]
    assert "asw start" in command[6]
    assert "--no-commit" in command[6]
    assert "--restart" in command[6]


def test_runner_invokes_devcontainer_flow_and_syncs_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful run should boot, exec, sync outputs, and clean the container up."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / ".devcontainer").mkdir()
    (repo_root / ".devcontainer" / "devcontainer.json").write_text("{}", encoding="utf-8")
    (repo_root / "scripts").mkdir()
    (repo_root / "scripts" / "test_company.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (repo_root / "src").mkdir()
    (repo_root / "src" / "feature.py").write_text("print('ok')\n", encoding="utf-8")
    (repo_root / "tests").mkdir()
    (repo_root / "tests" / "test_vision.md").write_text("# Vision\n", encoding="utf-8")

    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    monkeypatch.setattr("asw.test_company_runner.tempfile.mkdtemp", lambda prefix: str(scratch_root))
    monkeypatch.setattr("asw.test_company_runner.shutil.which", lambda name: f"/usr/bin/{name}")

    config = IsolatedTestCompanyConfig(repo_root=repo_root)
    runner = IsolatedTestCompanyRunner(
        config,
        env={"PATH": os.environ.get("PATH", ""), "GEMINI_API_KEY": "test-key"},
    )

    commands: list[list[str]] = []

    def fake_run(command: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        _ = env
        commands.append(command)
        if command[:2] == ["devcontainer", "up"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout='{"outcome":"success","containerId":"container-123"}\n',
                stderr="",
            )

        if command[:2] == ["devcontainer", "exec"]:
            nested_output = scratch_root / "repo" / "tests" / "test_company"
            nested_output.mkdir(parents=True, exist_ok=True)
            (nested_output / "debug.log").write_text("nested debug\n", encoding="utf-8")
            (nested_output / ".company").mkdir()
            (nested_output / ".company" / "pipeline_state.json").write_text("{}", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

        if command[:3] == ["docker", "rm", "-f"]:
            return subprocess.CompletedProcess(command, 0, stdout="removed\n", stderr="")

        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr(runner, "_run_command", fake_run)

    exit_code = runner.run()

    assert exit_code == 0
    assert commands[0][:2] == ["devcontainer", "up"]
    assert commands[1][:2] == ["devcontainer", "exec"]
    assert commands[2][:3] == ["docker", "rm", "-f"]
    assert (repo_root / "tests" / "test_company" / "debug.log").read_text(encoding="utf-8") == "nested debug\n"
    assert (repo_root / "tests" / "test_company" / ".company" / "pipeline_state.json").is_file()
    assert not scratch_root.exists()
