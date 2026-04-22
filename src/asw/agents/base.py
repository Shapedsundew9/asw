"""Base agent class that binds a role system-prompt to an LLM backend."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

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

    def _load_system_prompt(self) -> str:
        """Return the role prompt with any assigned standards appended."""
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

        return system_prompt

    def _build_user_prompt(
        self,
        context: dict[str, str],
        *,
        feedback: str | None = None,
        plan: str | None = None,
    ) -> str:
        """Return the assembled user prompt for the current invocation."""

        parts: list[str] = []
        for key, value in context.items():
            parts.append(f"### {key.upper()}\n\n{value}")

        if plan is not None:
            parts.append(f"### APPROVED IMPLEMENTATION PLAN\n\n{plan}")

        if feedback:
            parts.append(f"### REVIEWER FEEDBACK\n\n{feedback}")

        user_prompt = "\n\n".join(parts)
        context_summary_items = [f"{key}={len(value)} chars" for key, value in context.items()]
        if plan is not None:
            context_summary_items.append(f"plan={len(plan)} chars")
        context_summary = ", ".join(context_summary_items) or "(none)"
        logger.debug(
            "Agent %s assembled user prompt (%d chars, sha256=%s, context=[%s], feedback=%s)",
            self.name,
            len(user_prompt),
            _checksum_prefix(user_prompt),
            context_summary,
            "yes" if feedback else "no",
        )

        return user_prompt

    def _invoke(
        self,
        context: dict[str, str],
        *,
        mode: str,
        feedback: str | None = None,
        plan: str | None = None,
        auto_approve: bool = True,
    ) -> str:
        """Invoke the backend for the requested mode and return the raw response."""
        system_prompt = self._load_system_prompt()
        user_prompt = self._build_user_prompt(context, feedback=feedback, plan=plan)

        if mode == "plan":
            response = cast(str, self.llm.invoke_plan(system_prompt, user_prompt))
        elif mode == "execute":
            response = cast(str, self.llm.invoke_execute(system_prompt, user_prompt, auto_approve=auto_approve))
        else:
            response = cast(str, self.llm.invoke(system_prompt, user_prompt))

        logger.debug(
            "Agent %s received %s response (%d chars, sha256=%s)",
            self.name,
            mode,
            len(response),
            _checksum_prefix(response),
        )
        return response

    def run(self, context: dict[str, str], *, feedback: str | None = None) -> str:
        """Execute the agent's standard text-generation flow.

        Args:
            context: Key/value pairs injected into the user prompt.
            feedback: Optional reviewer feedback from a prior rejected attempt.

        Returns:
            The raw text output from the LLM.
        """
        return self._invoke(context, mode="invoke", feedback=feedback)

    def plan(self, context: dict[str, str], *, feedback: str | None = None) -> str:
        """Execute the agent in planning mode.

        Args:
            context: Key/value pairs injected into the user prompt.
            feedback: Optional reviewer feedback from a prior rejected attempt.

        Returns:
            The raw planning text output from the LLM.
        """
        return self._invoke(context, mode="plan", feedback=feedback)

    def execute(
        self,
        context: dict[str, str],
        plan: str,
        *,
        auto_approve: bool = True,
        feedback: str | None = None,
    ) -> str:
        """Execute the agent in implementation mode.

        Args:
            context: Key/value pairs injected into the user prompt.
            plan: The approved implementation plan to execute.
            auto_approve: Whether the backend should use auto-approve execution mode when supported.
            feedback: Optional reviewer feedback from a prior rejected attempt.

        Returns:
            The raw execution text output from the LLM.
        """
        return self._invoke(
            context,
            mode="execute",
            feedback=feedback,
            plan=plan,
            auto_approve=auto_approve,
        )
