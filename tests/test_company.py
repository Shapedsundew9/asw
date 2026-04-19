"""Tests for .company/ directory initialisation."""

from __future__ import annotations

from pathlib import Path

from asw.company import (
    COMPANY_DIR,
    FAILED_ARTIFACTS_DIR,
    SUBDIRS,
    clear_company,
    hash_file,
    init_company,
    mark_phase_complete,
    new_pipeline_state,
    read_pipeline_state,
    write_failed_artifact,
    write_pipeline_state,
)


def test_init_creates_directories(tmp_path: Path) -> None:
    """Test that init_company creates all expected subdirectories."""
    company = init_company(tmp_path)

    assert company == tmp_path / COMPANY_DIR
    for subdir in SUBDIRS:
        assert (company / subdir).is_dir()


def test_init_copies_role_templates(tmp_path: Path) -> None:
    """Test that init_company copies role template files."""
    company = init_company(tmp_path)

    roles_dir = company / "roles"
    role_files = list(roles_dir.glob("*.md"))
    assert len(role_files) >= 5

    names = {f.name for f in role_files}
    assert "cpo.md" in names
    assert "cto.md" in names
    assert "vpe.md" in names


def test_init_copies_templates(tmp_path: Path) -> None:
    """Test that init_company copies bundled template files."""
    company = init_company(tmp_path)

    templates_dir = company / "templates"
    template_files = {f.name for f in templates_dir.glob("*.md")}
    assert "prd_template.md" in template_files
    assert "architecture_template.md" in template_files
    assert "execution_plan_template.md" in template_files
    assert "role_template.md" in template_files


def test_init_copies_standards(tmp_path: Path) -> None:
    """Test that init_company copies bundled standards files."""
    company = init_company(tmp_path)

    standards_dir = company / "standards"
    standards_files = {f.name for f in standards_dir.glob("*.md")}
    assert "python_guidelines.md" in standards_files
    assert "ui_guidelines.md" in standards_files


def test_init_migrates_state_to_memory(tmp_path: Path) -> None:
    """Test that init_company renames legacy state/ to memory/."""
    # Pre-create a legacy .company/state/ directory with a file.
    legacy_state = tmp_path / COMPANY_DIR / "state"
    legacy_state.mkdir(parents=True)
    (legacy_state / "some_file.txt").write_text("legacy data")

    company = init_company(tmp_path)

    # state/ should be gone, memory/ should exist with the file.
    assert not (company / "state").exists()
    assert (company / "memory").is_dir()
    assert (company / "memory" / "some_file.txt").read_text() == "legacy data"


def test_init_idempotent(tmp_path: Path) -> None:
    """Test that init_company can be called multiple times safely."""
    init_company(tmp_path)
    # Running again must not raise or overwrite existing files.
    company = init_company(tmp_path)
    assert (company / "roles" / "cpo.md").is_file()
    assert (company / "templates" / "prd_template.md").is_file()
    assert (company / "standards" / "python_guidelines.md").is_file()


# ── Pipeline state tests ────────────────────────────────────────────────


def test_hash_file_consistent(tmp_path: Path) -> None:
    """hash_file returns the same digest for the same content."""
    f = tmp_path / "test.txt"
    f.write_text("hello world")
    assert hash_file(f) == hash_file(f)


def test_hash_file_differs_on_change(tmp_path: Path) -> None:
    """hash_file returns a different digest when content changes."""
    f = tmp_path / "test.txt"
    f.write_text("version 1")
    h1 = hash_file(f)
    f.write_text("version 2")
    h2 = hash_file(f)
    assert h1 != h2


def test_read_pipeline_state_missing(tmp_path: Path) -> None:
    """read_pipeline_state returns None when no state file exists."""
    assert read_pipeline_state(tmp_path) is None


def test_read_pipeline_state_corrupt(tmp_path: Path) -> None:
    """read_pipeline_state returns None for invalid JSON."""
    state_path = tmp_path / COMPANY_DIR / "pipeline_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("not json!", encoding="utf-8")
    assert read_pipeline_state(tmp_path) is None


def test_write_and_read_pipeline_state(tmp_path: Path) -> None:
    """Round-trip: write then read pipeline state."""
    init_company(tmp_path)
    state = new_pipeline_state()
    write_pipeline_state(tmp_path, state)
    loaded = read_pipeline_state(tmp_path)
    assert loaded == state


def test_mark_phase_complete(tmp_path: Path) -> None:
    """mark_phase_complete records a phase and persists state."""
    init_company(tmp_path)
    state = new_pipeline_state()
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n")
    prd = tmp_path / COMPANY_DIR / "artifacts" / "prd.md"
    prd.write_text("# PRD\n")
    write_pipeline_state(tmp_path, state)

    state = mark_phase_complete(tmp_path, state, "prd", input_paths=[vision], output_paths=[prd])
    assert "prd" in state["phases"]
    assert "completed_at" in state["phases"]["prd"]
    assert state["phases"]["prd"]["inputs"]["vision.md"] == hash_file(vision)
    assert state["phases"]["prd"]["outputs"][".company/artifacts/prd.md"] == hash_file(prd)
    assert state["tracked_files"]["vision.md"] == hash_file(vision)
    assert state["tracked_files"][".company/artifacts/prd.md"] == hash_file(prd)

    # Verify it was persisted.
    loaded = read_pipeline_state(tmp_path)
    assert loaded is not None
    assert "prd" in loaded["phases"]


def test_clear_company(tmp_path: Path) -> None:
    """clear_company removes the entire .company/ directory."""
    company = init_company(tmp_path)
    assert company.is_dir()
    clear_company(tmp_path)
    assert not company.exists()


def test_clear_company_noop_if_missing(tmp_path: Path) -> None:
    """clear_company does nothing if .company/ doesn't exist."""
    clear_company(tmp_path)  # Should not raise.


def test_write_failed_artifact_persists_output_and_errors(tmp_path: Path) -> None:
    """Failed artifacts should be saved under .company/artifacts/failed/."""
    company = init_company(tmp_path)

    failed_path = write_failed_artifact(
        company,
        "PRD",
        "bad output",
        ["Missing section", "Bad checklist"],
        attempt=2,
    )

    assert failed_path.is_file()
    assert failed_path.parent == company / "artifacts" / FAILED_ARTIFACTS_DIR
    content = failed_path.read_text(encoding="utf-8")
    assert "# Failed PRD Output" in content
    assert "- Attempt: 2" in content
    assert "- Missing section" in content
    assert "- Bad checklist" in content
    assert "bad output" in content
