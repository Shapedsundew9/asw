"""Manage the .company/ directory – the agents' shared reality."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

logger = logging.getLogger("asw.company")

COMPANY_DIR = ".company"
SUBDIRS = ("roles", "artifacts", "memory", "templates", "standards")
PIPELINE_STATE_FILE = "pipeline_state.json"
FAILED_ARTIFACTS_DIR = "failed"


def new_pipeline_state() -> dict:
    """Return an empty pipeline state document."""
    return {
        "version": "0.3",
        "tracked_files": {},
        "phases": {},
    }


def _package_dir(name: str) -> Path:
    """Return the path to a bundled package sub-directory."""
    return Path(str(resources.files("asw").joinpath(name)))


def _copy_bundled(src_dir: Path, dest_dir: Path) -> None:
    """Copy files from *src_dir* into *dest_dir*, skipping existing files."""
    if not src_dir.is_dir():
        return
    for src_file in src_dir.iterdir():
        if not src_file.is_file():
            continue
        dest = dest_dir / src_file.name
        if not dest.exists():
            shutil.copy2(src_file, dest)
            logger.debug("Copied bundled file: %s -> %s", src_file, dest)


def _migrate_state_to_memory(company: Path) -> None:
    """Rename legacy .company/state/ to .company/memory/ if needed."""
    state_dir = company / "state"
    memory_dir = company / "memory"
    if state_dir.is_dir() and not memory_dir.exists():
        state_dir.rename(memory_dir)
        logger.info("Migrated .company/state/ -> .company/memory/")


def init_company(workdir: Path) -> Path:
    """Initialise the .company/ directory tree and copy bundled assets.

    Returns the resolved .company/ path.
    """
    company = workdir / COMPANY_DIR

    # Migrate legacy state/ -> memory/ before creating directories.
    _migrate_state_to_memory(company)

    for subdir in SUBDIRS:
        (company / subdir).mkdir(parents=True, exist_ok=True)
    logger.debug("Company directories ensured: %s", company)

    # Copy bundled assets into .company/ (skip files already present).
    _copy_bundled(_package_dir("roles"), company / "roles")
    _copy_bundled(_package_dir("templates"), company / "templates")
    _copy_bundled(_package_dir("standards"), company / "standards")

    return company


def hash_file(path: Path) -> str:
    """Return the SHA-256 hex digest of a file's contents."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def tracked_path_key(workdir: Path, path: Path) -> str:
    """Return a stable key for *path* relative to *workdir* when possible."""
    resolved_workdir = workdir.resolve()
    resolved_path = path.resolve()
    try:
        return resolved_path.relative_to(resolved_workdir).as_posix()
    except ValueError:
        return str(resolved_path)


def snapshot_paths(workdir: Path, paths: list[Path]) -> dict[str, str | None]:
    """Return the current hash snapshot for each tracked file path."""
    snapshot: dict[str, str | None] = {}
    for path in paths:
        snapshot[tracked_path_key(workdir, path)] = hash_file(path) if path.is_file() else None
    return snapshot


def update_tracked_files(state: dict, snapshot: dict[str, str | None]) -> None:
    """Merge a hash snapshot into the top-level tracked file catalog."""
    state.setdefault("tracked_files", {}).update(snapshot)


def read_pipeline_state(workdir: Path) -> dict | None:
    """Read ``.company/pipeline_state.json`` and return parsed dict.

    Returns *None* if the file is missing or contains invalid JSON.
    """
    state_path = workdir / COMPANY_DIR / PIPELINE_STATE_FILE
    if not state_path.is_file():
        return None
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError, OSError:
        logger.warning("Could not read pipeline state file: %s", state_path)
        return None


def write_pipeline_state(workdir: Path, state: dict) -> None:
    """Atomically write pipeline state to ``.company/pipeline_state.json``."""
    state_path = workdir / COMPANY_DIR / PIPELINE_STATE_FILE
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_text = json.dumps(state, indent=2) + "\n"
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=state_path.parent,
            prefix=f".{state_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(state_text)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        temp_path.replace(state_path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
    logger.debug("Pipeline state written: %s", state_path)


def write_failed_artifact(
    company: Path,
    phase_name: str,
    raw_output: str,
    errors: list[str],
    *,
    attempt: int,
) -> Path:
    """Persist a mechanically invalid artifact for later inspection."""
    failed_dir = company / "artifacts" / FAILED_ARTIFACTS_DIR
    failed_dir.mkdir(parents=True, exist_ok=True)

    slug = re.sub(r"[^a-z0-9]+", "_", phase_name.lower()).strip("_") or "phase"
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
    failed_path = failed_dir / f"{slug}_attempt{attempt}_{stamp}.md"

    lines = [
        f"# Failed {phase_name} Output",
        "",
        f"- Attempt: {attempt}",
        f"- Saved At: {datetime.now(tz=timezone.utc).isoformat()}",
        "",
        "## Validation Errors",
        "",
    ]
    lines.extend(f"- {err}" for err in errors)
    lines.extend(
        [
            "",
            "## Raw LLM Output",
            "",
            "```text",
            raw_output,
            "```",
            "",
        ]
    )

    failed_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Failed artifact written: %s", failed_path)
    return failed_path


def mark_phase_complete(  # pylint: disable=too-many-arguments
    workdir: Path,
    state: dict,
    phase: str,
    *,
    input_paths: list[Path] | None = None,
    output_paths: list[Path] | None = None,
    metadata: dict[str, object] | None = None,
) -> dict:
    """Record a phase as completed in *state* and persist to disk.

    Returns the updated state dict.
    """
    input_snapshot = snapshot_paths(workdir, input_paths or [])
    output_snapshot = snapshot_paths(workdir, output_paths or [])
    update_tracked_files(state, input_snapshot)
    update_tracked_files(state, output_snapshot)

    phase_record: dict[str, object] = {
        "completed_at": datetime.now(tz=timezone.utc).isoformat(),
        "inputs": input_snapshot,
        "outputs": output_snapshot,
    }
    if metadata:
        phase_record["metadata"] = metadata

    state.setdefault("phases", {})[phase] = phase_record
    write_pipeline_state(workdir, state)
    return state


def clear_company(workdir: Path) -> None:
    """Delete the entire ``.company/`` directory for a clean restart."""
    company = workdir / COMPANY_DIR
    if company.is_dir():
        shutil.rmtree(company)
        logger.info("Removed company directory: %s", company)
