"""Exception types shared by LLM backend implementations."""

from __future__ import annotations


class LLMInvocationError(RuntimeError):
    """Raised when an LLM backend invocation fails."""

    def __init__(self, message: str, *, retryable: bool = False, reason: str | None = None) -> None:
        """Initialise the invocation error with retry metadata."""
        super().__init__(message)
        self.retryable = retryable
        self.reason = reason


class TransientLLMError(LLMInvocationError):
    """Raised for backend failures that are safe to retry."""

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        """Initialise a retryable LLM invocation error."""
        super().__init__(message, retryable=True, reason=reason)
