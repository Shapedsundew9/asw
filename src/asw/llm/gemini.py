"""Gemini CLI subprocess wrapper."""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Iterable

logger = logging.getLogger("asw.llm.gemini")

_DEFAULT_TIMEOUT = 300  # seconds


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
        combined_prompt = f"SYSTEM:\n{system_prompt}\n\nUSER:\n{user_prompt}"

        cmd: list[str] = ["gemini", "-p", combined_prompt, "-o", "json"]
        if self._model:
            cmd.extend(["-m", self._model])

        logger.debug("Gemini CLI command: %s", cmd[:2] + ["<prompt>"] + cmd[3:])
        logger.debug("Gemini CLI combined prompt (%d chars):\n%s", len(combined_prompt), combined_prompt)
        logger.debug("Timeout: %d seconds, model: %s", self._timeout, self._model or "(default)")

        result = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            timeout=self._timeout,
            check=False,
        )

        logger.debug("Gemini CLI exit code: %d", result.returncode)
        if result.stderr:
            logger.debug("Gemini CLI stderr:\n%s", result.stderr)
        logger.debug("Gemini CLI raw stdout (%d chars):\n%s", len(result.stdout), result.stdout)

        if result.returncode != 0:
            msg = f"Gemini CLI exited with code {result.returncode}:\n{result.stderr.strip()}"
            raise RuntimeError(msg)

        extracted = self._extract_text(result.stdout)
        logger.debug("Gemini extracted response (%d chars):\n%s", len(extracted), extracted)
        return extracted

    @staticmethod
    def _extract_text(raw: str) -> str:
        """Extract the model response text from Gemini JSON output."""
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and "response" in parsed:
                return str(parsed["response"])

            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict) and "response" in item:
                        return str(item["response"])

            for data in GeminiCLIBackend._iter_json_lines(raw):
                if isinstance(data, dict) and "response" in data:
                    return str(data["response"])
            return raw
        except json.JSONDecodeError, KeyError:
            for data in GeminiCLIBackend._iter_json_lines(raw):
                if isinstance(data, dict) and "response" in data:
                    return str(data["response"])
            return raw

    @staticmethod
    def _iter_json_lines(raw: str) -> Iterable[dict | list | str | int | float | bool | None]:
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
