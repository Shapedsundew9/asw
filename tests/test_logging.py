"""Tests for debug logging signal-to-noise behavior."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from asw.agents.base import Agent
from asw.llm.gemini import GeminiCLIBackend


def test_agent_run_logs_prompt_metadata_without_dumping_source_files(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Agent logs should summarize prompt sources instead of dumping their full text."""
    role_file = tmp_path / "role.md"
    role_file.write_text("# Role\n\nSECRET ROLE BODY\n", encoding="utf-8")
    standard_file = tmp_path / "standard.md"
    standard_file.write_text("SECRET STANDARD BODY\n", encoding="utf-8")
    llm = MagicMock()
    llm.invoke.return_value = "SECRET RESPONSE BODY"

    agent = Agent(name="CPO", role_file=role_file, llm=llm, standards=[standard_file])

    with caplog.at_level(logging.DEBUG, logger="asw.agents"):
        agent.run({"vision": "SECRET VISION BODY"})

    assert "loading role file" in caplog.text
    assert "loading standard" in caplog.text
    assert "assembled system prompt" in caplog.text
    assert "assembled user prompt" in caplog.text
    assert "SECRET ROLE BODY" not in caplog.text
    assert "SECRET STANDARD BODY" not in caplog.text
    assert "SECRET VISION BODY" not in caplog.text
    assert "SECRET RESPONSE BODY" not in caplog.text


def test_gemini_logs_combined_prompt_once_and_does_not_dump_raw_stdout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Gemini debug logs should keep one full combined prompt and summarize raw stdout."""
    backend = GeminiCLIBackend()
    completed = subprocess.CompletedProcess(
        args=["gemini"],
        returncode=0,
        stdout='{"session_id": "abc123", "response": "Final markdown"}',
        stderr="",
    )

    with (
        patch("asw.llm.gemini.subprocess.run", return_value=completed),
        caplog.at_level(logging.DEBUG, logger="asw.llm.gemini"),
    ):
        result = backend.invoke("system text", "user text")

    assert result == "Final markdown"
    assert caplog.text.count("Gemini CLI combined prompt") == 1
    assert "SYSTEM:\nsystem text\n\nUSER:\nuser text" in caplog.text
    assert "Gemini CLI raw stdout" in caplog.text
    assert "Gemini extracted response" in caplog.text
    assert "Final markdown" in caplog.text
    assert '"session_id": "abc123"' not in caplog.text
