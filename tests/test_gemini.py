"""Tests for Gemini CLI output extraction and failure classification."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from asw.llm.errors import LLMInvocationError, TransientLLMError
from asw.llm.gemini import GeminiCLIBackend


def test_extract_text_from_pretty_printed_json_object() -> None:
    """Extract the response field from multi-line JSON output."""
    raw = """{
  \"session_id\": \"abc\",
  \"response\": \"## Executive Summary\\nHello\",
  \"stats\": {\"tokens\": 10}
}"""

    extracted = GeminiCLIBackend.extract_text(raw)
    assert extracted == "## Executive Summary\nHello"


def test_extract_text_from_ndjson_lines() -> None:
    """Extract response when output is newline-delimited JSON."""
    raw = '{"event":"start"}\n{"response":"Final markdown"}\n'

    extracted = GeminiCLIBackend.extract_text(raw)
    assert extracted == "Final markdown"


def test_extract_text_falls_back_to_raw_for_non_json() -> None:
    """Return the original content when no JSON can be parsed."""
    raw = "plain text output"

    extracted = GeminiCLIBackend.extract_text(raw)
    assert extracted == raw


def test_invoke_raises_transient_error_for_timeout() -> None:
    """CLI timeouts should be surfaced as retryable backend failures."""
    backend = GeminiCLIBackend(timeout=1)

    with (
        patch(
            "asw.llm.gemini.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["gemini"], timeout=1),
        ),
        pytest.raises(TransientLLMError) as exc_info,
    ):
        backend.invoke("system", "user")

    assert exc_info.value.retryable is True
    assert exc_info.value.reason == "timeout"


def test_invoke_raises_transient_error_for_service_unavailable() -> None:
    """Service-unavailable CLI failures should be retried."""
    backend = GeminiCLIBackend()
    completed = subprocess.CompletedProcess(
        args=["gemini"],
        returncode=1,
        stdout="",
        stderr="503 Service Unavailable",
    )

    with (
        patch("asw.llm.gemini.subprocess.run", return_value=completed),
        pytest.raises(TransientLLMError) as exc_info,
    ):
        backend.invoke("system", "user")

    assert exc_info.value.retryable is True
    assert exc_info.value.reason == "service-unavailable"


def test_invoke_raises_non_retryable_error_for_cli_failure() -> None:
    """Non-transient CLI failures should fail fast without retry classification."""
    backend = GeminiCLIBackend()
    completed = subprocess.CompletedProcess(
        args=["gemini"],
        returncode=2,
        stdout="",
        stderr="Unknown option --bad-flag",
    )

    with (
        patch("asw.llm.gemini.subprocess.run", return_value=completed),
        pytest.raises(LLMInvocationError) as exc_info,
    ):
        backend.invoke("system", "user")

    assert exc_info.value.retryable is False
    assert exc_info.value.reason == "non-transient-cli-error"
