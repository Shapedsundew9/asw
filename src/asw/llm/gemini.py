"""Gemini CLI subprocess wrapper."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

_DEFAULT_TIMEOUT = 300  # seconds


class GeminiCLIBackend:
    """Call the Gemini CLI in non-interactive (headless) mode."""

    def __init__(self, *, timeout: int = _DEFAULT_TIMEOUT, model: str | None = None) -> None:
        self._timeout = timeout
        self._model = model

    def invoke(self, system_prompt: str, user_prompt: str) -> str:
        """Run Gemini CLI with *system_prompt* + *user_prompt* and return the text response."""
        combined_prompt = f"SYSTEM:\n{system_prompt}\n\nUSER:\n{user_prompt}"

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as fh:
            fh.write(combined_prompt)
            prompt_file = Path(fh.name)

        try:
            cmd: list[str] = ["gemini", "-p", combined_prompt, "-o", "json"]
            if self._model:
                cmd.extend(["-m", self._model])

            result = subprocess.run(  # noqa: S603
                cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        finally:
            prompt_file.unlink(missing_ok=True)

        if result.returncode != 0:
            msg = f"Gemini CLI exited with code {result.returncode}:\n{result.stderr.strip()}"
            raise RuntimeError(msg)

        return self._extract_text(result.stdout)

    @staticmethod
    def _extract_text(raw: str) -> str:
        """Extract the model response text from Gemini JSON output."""
        try:
            for line in raw.strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                if isinstance(data, dict) and "response" in data:
                    return str(data["response"])
            return raw
        except json.JSONDecodeError, KeyError:
            return raw
