"""Tests for Gemini CLI output extraction."""

from __future__ import annotations

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
