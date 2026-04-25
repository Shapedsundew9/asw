"""Run the test-company workflow inside a disposable dev container."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence
from uuid import uuid4

REQUIRED_ENV_VARS: tuple[str, ...] = ("GEMINI_API_KEY",)
SNAPSHOT_EXCLUDED_NAMES: frozenset[str] = frozenset(
    {
        ".company",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
    }
)
SNAPSHOT_EXCLUDED_PATHS: frozenset[Path] = frozenset({Path("tests/test_company")})


class IsolatedTestCompanyError(RuntimeError):
    """Raised when the isolated test-company runner cannot proceed."""


@dataclass(frozen=True)
class IsolatedTestCompanyConfig:
    """Configuration for one isolated test-company run."""

    repo_root: Path
    vision_path: Path = Path("tests/test_vision.md")
    workdir: Path = Path("tests/test_company")
    debug_log: Path = Path("tests/test_company/debug.log")
    host_output_dir: Path = Path("tests/test_company")
    required_env_vars: tuple[str, ...] = REQUIRED_ENV_VARS
    keep_workspace: bool = False


def default_repo_root() -> Path:
    """Return the repository root for the installed source tree."""
    return Path(__file__).resolve().parents[2]


def collect_required_env(
    env: Mapping[str, str],
    required_vars: Sequence[str] = REQUIRED_ENV_VARS,
) -> dict[str, str]:
    """Return the allowlisted environment variables required for the run."""
    missing = [name for name in required_vars if not env.get(name)]
    if missing:
        missing_list = ", ".join(sorted(missing))
        raise IsolatedTestCompanyError(
            "Missing required environment variable(s) for isolated test_company run: " f"{missing_list}"
        )

    return {name: env[name] for name in required_vars}


def copy_workspace_snapshot(source_root: Path, destination_root: Path) -> None:
    """Copy the repository into a disposable workspace snapshot."""
    source_root = source_root.resolve()

    def ignore(directory: str, names: list[str]) -> set[str]:
        rel_dir = Path(directory).resolve().relative_to(source_root)
        ignored: set[str] = set()

        for name in names:
            rel_path = Path(name) if rel_dir == Path(".") else rel_dir / name
            if name in SNAPSHOT_EXCLUDED_NAMES or rel_path in SNAPSHOT_EXCLUDED_PATHS:
                ignored.add(name)

        return ignored

    shutil.copytree(source_root, destination_root, ignore=ignore)
    (destination_root / "tests" / "test_company").mkdir(parents=True, exist_ok=True)


def build_start_command(config: IsolatedTestCompanyConfig) -> list[str]:
    """Return the inner ``asw start`` command for the isolated workspace."""
    return [
        "asw",
        "start",
        "--vision",
        config.vision_path.as_posix(),
        "--workdir",
        config.workdir.as_posix(),
        "--debug",
        config.debug_log.as_posix(),
        "--no-commit",
        "--restart",
    ]


def build_devcontainer_up_command(workspace_folder: Path) -> list[str]:
    """Return the command that boots the disposable dev container."""
    return ["devcontainer", "up", "--workspace-folder", str(workspace_folder)]


def build_devcontainer_exec_command(workspace_folder: Path, inner_command: Sequence[str]) -> list[str]:
    """Return the command that executes the test-company run in the container."""
    return [
        "devcontainer",
        "exec",
        "--workspace-folder",
        str(workspace_folder),
        "bash",
        "-lc",
        shlex.join(str(part) for part in inner_command),
    ]


def extract_container_id(stdout: str) -> str:
    """Extract the container id from ``devcontainer up`` output."""
    for line in reversed(stdout.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        container_id = payload.get("containerId") if isinstance(payload, dict) else None
        if isinstance(container_id, str) and container_id:
            return container_id

    raise IsolatedTestCompanyError("Could not find a containerId in devcontainer up output.")


def sync_output_directory(source_dir: Path, destination_dir: Path) -> None:
    """Mirror the nested ``tests/test_company`` output back into the live checkout."""
    destination_dir.mkdir(parents=True, exist_ok=True)

    for stale_name in (".company", ".devcontainer", "debug.log"):
        stale_path = destination_dir / stale_name
        if stale_path.is_dir():
            shutil.rmtree(stale_path)
        elif stale_path.exists():
            stale_path.unlink()

    if not source_dir.exists():
        return

    for item in source_dir.iterdir():
        target = destination_dir / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
            continue
        shutil.copy2(item, target)


class IsolatedTestCompanyRunner:
    """Run ``scripts/test_company.sh`` logic in a disposable dev container."""

    def __init__(
        self,
        config: IsolatedTestCompanyConfig,
        *,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config
        self.env = dict(os.environ if env is None else env)

    def run(self) -> int:
        """Execute the isolated test-company workflow and return its exit code."""
        self._ensure_required_tools()
        allowed_env = collect_required_env(self.env, self.config.required_env_vars)
        command_env = dict(self.env)
        command_env.update(allowed_env)

        repo_root = self.config.repo_root.resolve()
        host_output_dir = self._resolve_repo_path(self.config.host_output_dir)
        log_dir = host_output_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        run_id = uuid4().hex[:12]
        scratch_root = Path(tempfile.mkdtemp(prefix=f"asw-test-company-{run_id}-"))
        workspace_copy = scratch_root / repo_root.name
        container_id: str | None = None

        try:
            copy_workspace_snapshot(repo_root, workspace_copy)
            nested_output_dir = workspace_copy / self.config.workdir
            nested_output_dir.mkdir(parents=True, exist_ok=True)

            up_command = build_devcontainer_up_command(workspace_copy)
            up_result = self._run_command(up_command, env=command_env)
            self._write_command_log(log_dir / f"isolated-{run_id}-up.log", up_command, up_result)
            if up_result.returncode != 0:
                raise IsolatedTestCompanyError(
                    "Failed to start the disposable dev container. "
                    f"See {log_dir / f'isolated-{run_id}-up.log'} for details."
                )
            container_id = extract_container_id(up_result.stdout)

            inner_command = build_start_command(self.config)
            exec_command = build_devcontainer_exec_command(workspace_copy, inner_command)
            exec_result = self._run_command(exec_command, env=command_env)
            self._write_command_log(log_dir / f"isolated-{run_id}-exec.log", exec_command, exec_result)

            sync_output_directory(nested_output_dir, host_output_dir)
            return exec_result.returncode
        finally:
            self._cleanup(run_id, log_dir, scratch_root, container_id)

    def _cleanup(self, run_id: str, log_dir: Path, scratch_root: Path, container_id: str | None) -> None:
        """Remove the disposable container and workspace snapshot."""
        if container_id:
            cleanup_command = ["docker", "rm", "-f", container_id]
            cleanup_result = self._run_command(cleanup_command, env=self.env)
            self._write_command_log(log_dir / f"isolated-{run_id}-cleanup.log", cleanup_command, cleanup_result)

        if not self.config.keep_workspace:
            shutil.rmtree(scratch_root, ignore_errors=True)

    def _ensure_required_tools(self) -> None:
        """Fail fast when the outer dev container cannot launch nested containers."""
        missing = [name for name in ("devcontainer", "docker") if shutil.which(name) is None]
        if missing:
            missing_list = ", ".join(sorted(missing))
            raise IsolatedTestCompanyError(
                "Missing required tool(s) for isolated test_company run: "
                f"{missing_list}. Rebuild the dev container after installing the nested-container prerequisites."
            )

    def _resolve_repo_path(self, path: Path) -> Path:
        """Resolve *path* relative to the configured repository root when needed."""
        if path.is_absolute():
            return path.resolve()
        return (self.config.repo_root / path).resolve()

    def _run_command(self, command: Sequence[str], *, env: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
        """Execute *command* and return the completed process."""
        return subprocess.run(
            list(command),
            capture_output=True,
            check=False,
            env=dict(env),
            text=True,
        )

    @staticmethod
    def _write_command_log(
        log_path: Path,
        command: Sequence[str],
        result: subprocess.CompletedProcess[str],
    ) -> None:
        """Persist the command, exit code, and captured output for one step."""
        content = "\n".join(
            [
                f"Command: {shlex.join(str(part) for part in command)}",
                f"Exit Code: {result.returncode}",
                "",
                "STDOUT:",
                result.stdout.rstrip(),
                "",
                "STDERR:",
                result.stderr.rstrip(),
                "",
            ]
        )
        log_path.write_text(content, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for isolated test-company runs."""
    parser = argparse.ArgumentParser(
        prog="python -m asw.test_company_runner",
        description="Run test_company inside a disposable dev container.",
    )
    parser.add_argument(
        "--keep-workspace",
        action="store_true",
        help="Preserve the copied disposable workspace after the run for debugging.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for the isolated test-company runner."""
    args = build_parser().parse_args(argv)
    config = IsolatedTestCompanyConfig(repo_root=default_repo_root(), keep_workspace=args.keep_workspace)

    try:
        return IsolatedTestCompanyRunner(config).run()
    except IsolatedTestCompanyError as exc:
        print(f"Error: {exc}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
