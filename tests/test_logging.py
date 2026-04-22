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


def test_agent_plan_delegates_to_backend_planning_method(tmp_path: Path) -> None:
    """Planning calls should route through the backend's planning method."""
    role_file = tmp_path / "role.md"
    role_file.write_text("# Role\n\nROLE BODY\n", encoding="utf-8")
    llm = MagicMock()
    llm.invoke_plan.return_value = "planned"

    agent = Agent(name="CPO", role_file=role_file, llm=llm)

    result = agent.plan({"vision": "Build it"})

    assert result == "planned"
    llm.invoke_plan.assert_called_once()
    llm.invoke.assert_not_called()
    llm.invoke_execute.assert_not_called()
    _, user_prompt = llm.invoke_plan.call_args.args
    assert "### VISION" in user_prompt


def test_agent_execute_delegates_plan_and_feedback_to_backend(tmp_path: Path) -> None:
    """Execution calls should include the approved plan, feedback, and auto-approve flag."""
    role_file = tmp_path / "role.md"
    role_file.write_text("# Role\n\nROLE BODY\n", encoding="utf-8")
    llm = MagicMock()
    llm.invoke_execute.return_value = "executed"

    agent = Agent(name="CPO", role_file=role_file, llm=llm)

    result = agent.execute(
        {"task": "Implement feature"},
        "1. Add the code",
        auto_approve=False,
        feedback="Fix the missing tests.",
    )

    assert result == "executed"
    llm.invoke_execute.assert_called_once()
    llm.invoke.assert_not_called()
    llm.invoke_plan.assert_not_called()
    _, user_prompt = llm.invoke_execute.call_args.args
    assert "### TASK" in user_prompt
    assert "### APPROVED IMPLEMENTATION PLAN" in user_prompt
    assert "1. Add the code" in user_prompt
    assert "### REVIEWER FEEDBACK" in user_prompt
    assert llm.invoke_execute.call_args.kwargs["auto_approve"] is False


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
