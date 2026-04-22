"""Definitions for immutable core project roles."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CoreRole:
    """Immutable project role contract used across planning and hiring."""

    title: str
    filename: str


DEVELOPMENT_LEAD = CoreRole(title="Development Lead", filename="development_lead.md")
DEVOPS_ENGINEER = CoreRole(title="DevOps Engineer", filename="devops_engineer.md")

MANDATORY_CORE_ROLES: tuple[CoreRole, ...] = (
    DEVELOPMENT_LEAD,
    DEVOPS_ENGINEER,
)
MANDATORY_CORE_ROLE_TITLES: tuple[str, ...] = tuple(role.title for role in MANDATORY_CORE_ROLES)
MANDATORY_CORE_ROLE_FILENAMES = frozenset(role.filename for role in MANDATORY_CORE_ROLES)
MANDATORY_CORE_ROLE_FILENAME_BY_TITLE = {role.title: role.filename for role in MANDATORY_CORE_ROLES}
