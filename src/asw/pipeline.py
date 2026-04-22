"""Shared pipeline execution types and helpers."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from asw.llm.backend import LLMBackend


@dataclass(frozen=True)
class PipelineRunOptions:
    """Runtime options that affect pipeline execution."""

    no_commit: bool = False
    stage_all: bool = False
    debug: bool = False
    restart: bool = False
    execute_phase_setups: bool = False


@dataclass
class PipelineExecutionContext:
    """Shared context for phase execution helpers."""

    state: dict
    company: Path
    vision_path: Path
    vision_content: str
    llm: LLMBackend
    options: PipelineRunOptions

    @property
    def workdir(self) -> Path:
        """Return the working directory for this pipeline run."""
        return self.company.parent


def string_checksum_prefix(content: str) -> str:
    """Return a short checksum prefix for log correlation."""
    return sha256(content.encode("utf-8")).hexdigest()[:12]
