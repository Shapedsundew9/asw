"""Gemini CLI subprocess wrapper."""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from collections.abc import Iterable

from asw.llm.errors import LLMInvocationError, TransientLLMError

logger = logging.getLogger("asw.llm.gemini")

_DEFAULT_TIMEOUT = 300  # seconds
_PLAN_PROMPT_PREAMBLE = (
    "MODE: PLAN\n" "Return a plan only. Do not describe completed execution or claim that files already changed.\n\n"
)
_EXECUTE_PROMPT_PREAMBLE = (
    "MODE: EXECUTE\n"
    "AUTO_APPROVE: {auto_approve}\n"
    "Carry out the requested changes using the approved plan and report what happened concisely.\n\n"
)


def _checksum_prefix(content: str) -> str:
    """Return a short checksum prefix for log correlation."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]


class GeminiCLIBackend:
    """Call the Gemini CLI in non-interactive (headless) mode."""

    def __init__(self, *, timeout: int = _DEFAULT_TIMEOUT, model: str | None = None) -> None:
        """Initialize the backend with an optional timeout and model name.

        Args:
            timeout: Maximum seconds to wait for the CLI process to complete.
            model: Gemini model name to pass via ``-m``; uses the CLI default if ``None``.
        """
        self._timeout = timeout
        self._model = model

    def invoke(self, system_prompt: str, user_prompt: str) -> str:
        """Run Gemini CLI with *system_prompt* + *user_prompt* and return the text response."""
        return self._invoke_mode(system_prompt, user_prompt, mode="invoke")

    def invoke_plan(self, system_prompt: str, user_prompt: str) -> str:
        """Run Gemini CLI in planning mode using prompt-level fallback instructions."""
        planning_prompt = f"{_PLAN_PROMPT_PREAMBLE}{user_prompt}"
        return self._invoke_mode(system_prompt, planning_prompt, mode="plan")

    def invoke_execute(self, system_prompt: str, user_prompt: str, *, auto_approve: bool = True) -> str:
        """Run Gemini CLI in execution mode using prompt-level fallback instructions."""
        execute_prompt = _EXECUTE_PROMPT_PREAMBLE.format(auto_approve=str(auto_approve).lower()) + user_prompt
        return self._invoke_mode(system_prompt, execute_prompt, mode="execute", auto_approve=auto_approve)

    def _invoke_mode(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        mode: str,
        auto_approve: bool | None = None,
    ) -> str:
        """Run Gemini CLI for a specific invocation mode and return the text response."""
        combined_prompt = self._combine_prompt(system_prompt, user_prompt)
        cmd = self._build_command(combined_prompt)

        logger.debug("Gemini CLI mode: %s", mode)
        logger.debug("Gemini CLI command: %s", cmd[:2] + ["<prompt>"] + cmd[3:])
        logger.debug(
            "Gemini CLI combined prompt (%d chars, sha256=%s):\n%s",
            len(combined_prompt),
            _checksum_prefix(combined_prompt),
            combined_prompt,
        )
        logger.debug(
            "Timeout: %d seconds, model: %s, auto_approve=%s",
            self._timeout,
            self._model or "(default)",
            auto_approve if auto_approve is not None else "(n/a)",
        )

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            msg = f"Gemini CLI timed out after {self._timeout} seconds."
            logger.warning("Gemini CLI transient failure: timeout")
            raise TransientLLMError(msg, reason="timeout") from exc

        logger.debug("Gemini CLI exit code: %d", result.returncode)
        if result.stderr:
            logger.debug("Gemini CLI stderr:\n%s", result.stderr)
        logger.debug(
            "Gemini CLI raw stdout (%d chars, sha256=%s)",
            len(result.stdout),
            _checksum_prefix(result.stdout),
        )

        if result.returncode != 0:
            msg = f"Gemini CLI exited with code {result.returncode}:\n{result.stderr.strip()}"
            retry_reason = self.classify_retryable_failure(result.stderr)
            if retry_reason is not None:
                logger.warning("Gemini CLI transient failure classified as: %s", retry_reason)
                raise TransientLLMError(msg, reason=retry_reason)
            raise LLMInvocationError(msg, reason="non-transient-cli-error")

        extracted = self.extract_text(result.stdout)
        logger.debug(
            "Gemini extracted response (%d chars, sha256=%s):\n%s",
            len(extracted),
            _checksum_prefix(extracted),
            extracted,
        )
        return extracted

    def _build_command(self, combined_prompt: str) -> list[str]:
        """Return the Gemini CLI command list for a prepared prompt."""
        cmd: list[str] = ["gemini", "-p", combined_prompt, "-o", "json"]
        if self._model:
            cmd.extend(["-m", self._model])
        return cmd

    @staticmethod
    def _combine_prompt(system_prompt: str, user_prompt: str) -> str:
        """Return a single Gemini prompt payload from system and user prompt text."""
        return f"SYSTEM:\n{system_prompt}\n\nUSER:\n{user_prompt}"

    @staticmethod
    def extract_text(raw: str) -> str:
        """Extract the model response text from Gemini JSON output."""
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and "response" in parsed:
                return str(parsed["response"])

            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict) and "response" in item:
                        return str(item["response"])

            for data in GeminiCLIBackend.iter_json_lines(raw):
                if isinstance(data, dict) and "response" in data:
                    return str(data["response"])
            return raw
        except json.JSONDecodeError, KeyError:
            for data in GeminiCLIBackend.iter_json_lines(raw):
                if isinstance(data, dict) and "response" in data:
                    return str(data["response"])
            return raw

    @staticmethod
    def iter_json_lines(
        raw: str,
    ) -> Iterable[dict | list | str | int | float | bool | None]:
        """Yield JSON values parsed from non-empty line chunks.

        Gemini may emit newline-delimited JSON in some modes, so this helper keeps
        line-by-line parsing as a fallback when whole-document parsing is not valid.
        """
        for line in raw.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue

    @staticmethod
    def classify_retryable_failure(stderr: str) -> str | None:
        """Return a retry reason for transient Gemini CLI failures, if any."""
        normalized = stderr.lower()
        retryable_patterns = {
            "rate-limit": ["rate limit", "too many requests", "429"],
            "service-unavailable": ["service unavailable", "503", "temporarily unavailable", "server busy"],
            "busy": ["try again later", "please retry", "busy"],
            "transport": [
                "connection reset",
                "connection refused",
                "connection aborted",
                "network error",
                "socket hang up",
                "econnreset",
                "econnrefused",
            ],
        }

        for reason, patterns in retryable_patterns.items():
            if any(pattern in normalized for pattern in patterns):
                return reason
        return None
