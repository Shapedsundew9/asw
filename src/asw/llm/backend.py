"""LLM backend protocol and factory."""

from __future__ import annotations

import logging
import shutil
from typing import Protocol, runtime_checkable

logger = logging.getLogger("asw.llm")


@runtime_checkable
class LLMBackend(Protocol):
    """Minimal interface every LLM backend must satisfy."""

    def invoke(self, system_prompt: str, user_prompt: str) -> str:
        """Send prompts to the model and return its text response."""
        return ""


def get_backend(name: str = "gemini") -> LLMBackend:
    """Return an :class:`LLMBackend` implementation by *name*.

    Raises:
    ------
    RuntimeError
        If the requested backend CLI tool is not found on ``$PATH``.
    ValueError
        If *name* is not recognised.
    """
    if name == "gemini":
        logger.debug("Resolving LLM backend: %s", name)
        if shutil.which("gemini") is None:
            msg = "The 'gemini' CLI was not found on $PATH. Install it with: npm install -g @google/gemini-cli"
            raise RuntimeError(msg)

        from asw.llm.gemini import GeminiCLIBackend

        logger.debug("GeminiCLIBackend instantiated")
        return GeminiCLIBackend()

    msg = f"Unknown LLM backend: {name!r}"
    raise ValueError(msg)
