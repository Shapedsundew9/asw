"""Base agent class that binds a role system-prompt to an LLM backend."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from asw.llm.backend import LLMBackend

logger = logging.getLogger("asw.agents")


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

        # Append organisational standards to the system prompt.
        for std_path in self.standards:
            if std_path.is_file():
                system_prompt += "\n\n---\n\n" + std_path.read_text(encoding="utf-8")
                logger.debug("Injected standard: %s", std_path)

        logger.debug(
            "Agent %s system prompt from %s (%d chars):\n%s",
            self.name,
            self.role_file,
            len(system_prompt),
            system_prompt,
        )

        parts: list[str] = []
        for key, value in context.items():
            parts.append(f"### {key.upper()}\n\n{value}")

        if feedback:
            parts.append(f"### REVIEWER FEEDBACK\n\n{feedback}")

        user_prompt = "\n\n".join(parts)
        logger.debug("Agent %s user prompt (%d chars):\n%s", self.name, len(user_prompt), user_prompt)
        response = self.llm.invoke(system_prompt, user_prompt)
        logger.debug("Agent %s LLM response (%d chars):\n%s", self.name, len(response), response)
        return response
