"""Tests for .company/ directory initialisation."""

from __future__ import annotations

from pathlib import Path

from asw.company import COMPANY_DIR, SUBDIRS, init_company


def test_init_creates_directories(tmp_path: Path) -> None:
    company = init_company(tmp_path)

    assert company == tmp_path / COMPANY_DIR
    for subdir in SUBDIRS:
        assert (company / subdir).is_dir()


def test_init_copies_role_templates(tmp_path: Path) -> None:
    company = init_company(tmp_path)

    roles_dir = company / "roles"
    role_files = list(roles_dir.glob("*.md"))
    assert len(role_files) >= 2  # cpo.md and cto.md at minimum

    names = {f.name for f in role_files}
    assert "cpo.md" in names
    assert "cto.md" in names


def test_init_idempotent(tmp_path: Path) -> None:
    init_company(tmp_path)
    # Running again must not raise or overwrite existing files.
    company = init_company(tmp_path)
    assert (company / "roles" / "cpo.md").is_file()
