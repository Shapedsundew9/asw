"""Manage the .company/ directory – the agents' shared reality."""

from __future__ import annotations

import logging
import shutil
from importlib import resources
from pathlib import Path

logger = logging.getLogger("asw.company")

COMPANY_DIR = ".company"
SUBDIRS = ("roles", "artifacts", "state")


def _package_roles_dir() -> Path:
    """Return the path to the bundled role templates."""
    return Path(str(resources.files("asw").joinpath("roles")))


def init_company(workdir: Path) -> Path:
    """Initialise the .company/ directory tree and copy role templates.

    Returns the resolved .company/ path.
    """
    company = workdir / COMPANY_DIR
    for subdir in SUBDIRS:
        (company / subdir).mkdir(parents=True, exist_ok=True)
    logger.debug("Company directories ensured: %s", company)

    # Copy bundled role templates into .company/roles/ (skip if already present).
    src_roles = _package_roles_dir()
    if src_roles.is_dir():
        for template in src_roles.iterdir():
            dest = company / "roles" / template.name
            if not dest.exists():
                shutil.copy2(template, dest)
                logger.debug("Copied role template: %s -> %s", template, dest)

    return company
