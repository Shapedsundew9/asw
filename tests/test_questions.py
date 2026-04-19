"""Tests for founder question extraction and local answer incorporation."""

from __future__ import annotations

import json

from asw.founder_questions import (
    _apply_founder_answers_to_prd,
    _extract_answered_founder_questions,
    _extract_founder_questions,
)


def test_extract_questions_no_json() -> None:
    """Question extraction should return None when no JSON block exists."""
    content = "# PRD\nNo questions here."
    assert _extract_founder_questions(content) is None


def test_extract_questions_wrong_json() -> None:
    """Question extraction should ignore JSON blocks without founder questions."""
    content = '# PRD\n```json\n{"foo": "bar"}\n```'
    assert _extract_founder_questions(content) is None


def test_extract_questions_success() -> None:
    """Question extraction should return founder questions from the JSON block."""
    questions = [{"question": "Database?", "choices": ["PG", "Lite"]}]
    content = f"# PRD\n```json\n{json.dumps({'founder_questions': questions})}\n```"
    assert _extract_founder_questions(content) == questions


def test_extract_questions_multiple_blocks() -> None:
    """Question extraction should find the first JSON block with founder questions."""
    questions = [{"question": "Color?"}]
    content = """
# PRD
```json
{"architecture": "info"}
```
Some text
```json
{"founder_questions": [{"question": "Color?"}]}
```
"""
    assert _extract_founder_questions(content) == questions


def test_extract_questions_permissive() -> None:
    """Question extraction should allow permissive fenced JSON formatting."""
    questions = [{"question": "Database?"}]
    content = f"```json {json.dumps({'founder_questions': questions})}```"
    assert _extract_founder_questions(content) == questions


def test_extract_questions_uppercase() -> None:
    """Question extraction should accept uppercase JSON fence labels."""
    questions = [{"question": "Database?"}]
    content = f"```JSON\n{json.dumps({'founder_questions': questions})}\n```"
    assert _extract_founder_questions(content) == questions


def test_extract_questions_ignores_answered_items() -> None:
    """Unanswered question extraction should skip already answered items."""
    content = """```json
{
  "founder_questions": [
    {"question": "Tree species?", "answer": "Oak"},
    {"question": "Wind animation?", "choices": ["Static", "Subtle"]}
  ]
}
```"""

    assert _extract_founder_questions(content) == [{"question": "Wind animation?", "choices": ["Static", "Subtle"]}]


def test_extract_answered_questions_returns_question_answer_pairs() -> None:
    """Answered question extraction should keep only resolved founder inputs."""
    content = """```json
{
  "founder_questions": [
    {"question": "Tree species?", "answer": "Oak"},
    {"question": "Wind animation?", "choices": ["Static", "Subtle"]}
  ]
}
```"""

    assert _extract_answered_founder_questions(content) == [{"question": "Tree species?", "answer": "Oak"}]


def test_apply_founder_answers_to_prd_updates_markdown_and_json() -> None:
    """Applying founder answers should update both PRD markdown and structured JSON."""
    content = """## Executive Summary

Example.

## Open Questions

1. Tree species?
   - Choices: ["Oak", "Birch"]
2. Wind animation?
   - Choices: ["Static", "Subtle"]

```json
{
  "founder_questions": [
    {"question": "Tree species?", "choices": ["Oak", "Birch"]},
    {"question": "Wind animation?", "choices": ["Static", "Subtle"]}
  ]
}
```
"""

    updated = _apply_founder_answers_to_prd(
        content,
        [
            {"question": "Tree species?", "answer": "Oak"},
            {"question": "Wind animation?", "answer": "Subtle"},
        ],
    )

    assert "- Answer: Oak" in updated
    assert "- Answer: Subtle" in updated
    assert '"answer": "Oak"' in updated
    assert '"answer": "Subtle"' in updated
    assert _extract_founder_questions(updated) is None
