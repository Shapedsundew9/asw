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


def test_invoke_plan_wraps_prompt_with_planning_preamble() -> None:
    """Planning mode should prepend explicit plan-only instructions to the user prompt."""
    backend = GeminiCLIBackend()
    completed = subprocess.CompletedProcess(
        args=["gemini"],
        returncode=0,
        stdout='{"response": "Plan"}',
        stderr="",
    )

    with patch("asw.llm.gemini.subprocess.run", return_value=completed) as mock_run:
        result = backend.invoke_plan("system text", "user text")

    assert result == "Plan"
    prompt = mock_run.call_args.args[0][2]
    assert "SYSTEM:\nsystem text" in prompt
    assert "MODE: PLAN" in prompt
    assert "Return a plan only." in prompt
    assert prompt.endswith("user text")


def test_invoke_execute_wraps_prompt_with_execution_preamble() -> None:
    """Execution mode should prepend explicit execution instructions and auto-approve state."""
    backend = GeminiCLIBackend()
    completed = subprocess.CompletedProcess(
        args=["gemini"],
        returncode=0,
        stdout='{"response": "Executed"}',
        stderr="",
    )

    with patch("asw.llm.gemini.subprocess.run", return_value=completed) as mock_run:
        result = backend.invoke_execute("system text", "user text", auto_approve=False)

    assert result == "Executed"
    prompt = mock_run.call_args.args[0][2]
    assert "SYSTEM:\nsystem text" in prompt
    assert "MODE: EXECUTE" in prompt
    assert "AUTO_APPROVE: false" in prompt
    assert "Carry out the requested changes" in prompt
    assert prompt.endswith("user text")


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
