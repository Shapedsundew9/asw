"""Base agent class that binds a role system-prompt to an LLM backend."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from asw.llm.backend import LLMBackend


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

    def run(self, context: dict[str, str], *, feedback: str | None = None) -> str:
        """Execute the agent's role against the supplied *context*.

        Parameters
        ----------
        context:
            Key/value pairs injected into the user prompt (e.g.
            ``{"vision": "<contents>"}``).
        feedback:
            Optional reviewer feedback from a prior rejected attempt.

        Returns
        -------
        str
            The raw text output from the LLM.
        """
        system_prompt = self.role_file.read_text(encoding="utf-8")

        parts: list[str] = []
        for key, value in context.items():
            parts.append(f"### {key.upper()}\n\n{value}")

        if feedback:
            parts.append(f"### REVIEWER FEEDBACK\n\n{feedback}")

        user_prompt = "\n\n".join(parts)
        return self.llm.invoke(system_prompt, user_prompt)
