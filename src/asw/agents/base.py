"""Base agent class that binds a role system-prompt to an LLM backend."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from asw.llm.backend import LLMBackend

logger = logging.getLogger("asw.agents")


def _checksum_prefix(content: str) -> str:
    """Return a short checksum prefix for log correlation."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]


@dataclass
class Agent:
    """A single LLM-backed agent with a fixed role.

    Parameters
    ----------
    name:
        Human-readable agent name (e.g. ``"CPO"``).
    role_file:
        Path to the Markdown file containing the system prompt for this role.
    llm:
        The :class:`LLMBackend` used to invoke the model.
    """

    name: str
    role_file: Path
    llm: LLMBackend
    standards: list[Path] = field(default_factory=list)

    def run(self, context: dict[str, str], *, feedback: str | None = None) -> str:
        """Execute the agent's role against the supplied *context*.

        Parameters
        ----------
        context:
            Key/value pairs injected into the user prompt (e.g.
            ``{"vision": "<contents>"}``).
        feedback:
            Optional reviewer feedback from a prior rejected attempt.

        Returns:
        -------
        str
            The raw text output from the LLM.
        """
        system_prompt = self.role_file.read_text(encoding="utf-8")
        logger.debug(
            "Agent %s loading role file %s (%d chars, sha256=%s)",
            self.name,
            self.role_file,
            len(system_prompt),
            _checksum_prefix(system_prompt),
        )

        # Append organisational standards to the system prompt.
        loaded_standards = 0
        for std_path in self.standards:
            if std_path.is_file():
                std_content = std_path.read_text(encoding="utf-8")
                system_prompt += "\n\n---\n\n" + std_content
                loaded_standards += 1
                logger.debug(
                    "Agent %s loading standard %s (%d chars, sha256=%s)",
                    self.name,
                    std_path,
                    len(std_content),
                    _checksum_prefix(std_content),
                )

        logger.debug(
            "Agent %s assembled system prompt (%d chars, standards=%d, sha256=%s)",
            self.name,
            len(system_prompt),
            loaded_standards,
            _checksum_prefix(system_prompt),
        )

        parts: list[str] = []
        for key, value in context.items():
            parts.append(f"### {key.upper()}\n\n{value}")

        if feedback:
            parts.append(f"### REVIEWER FEEDBACK\n\n{feedback}")

        user_prompt = "\n\n".join(parts)
        context_summary = ", ".join(f"{key}={len(value)} chars" for key, value in context.items()) or "(none)"
        logger.debug(
            "Agent %s assembled user prompt (%d chars, sha256=%s, context=[%s], feedback=%s)",
            self.name,
            len(user_prompt),
            _checksum_prefix(user_prompt),
            context_summary,
            "yes" if feedback else "no",
        )
        response = self.llm.invoke(system_prompt, user_prompt)
        logger.debug(
            "Agent %s received response (%d chars, sha256=%s)",
            self.name,
            len(response),
            _checksum_prefix(response),
        )
        return response
