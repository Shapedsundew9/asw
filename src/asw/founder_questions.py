"""Helpers for structured founder questions and local answer incorporation."""

from __future__ import annotations

import json
import re


def _extract_json_block(content: str) -> str | None:
    """Extract the first fenced JSON code block from *content*."""
    match = re.search(r"```json\s*(.*?)\s*```", content, re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else None


def _extract_founder_questions(content: str) -> list[dict] | None:
    """Return unresolved founder questions from the first matching JSON block."""
    unanswered = [item for item in _extract_founder_question_items(content) if "answer" not in item]
    return unanswered or None


def _find_founder_questions_block(content: str) -> tuple[dict, re.Match[str]] | None:
    """Return the first fenced JSON block containing founder questions."""
    for match in re.finditer(r"```json\s*(.*?)\s*```", content, re.DOTALL | re.IGNORECASE):
        block = match.group(1).strip()
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue

        if isinstance(data, dict) and isinstance(data.get("founder_questions"), list):
            return data, match
    return None


def _extract_founder_question_items(content: str) -> list[dict]:
    """Return all structured founder question items, including answered ones."""
    found = _find_founder_questions_block(content)
    if found is None:
        return []

    data, _ = found
    items: list[dict] = []
    for entry in data["founder_questions"]:
        if not isinstance(entry, dict):
            continue

        question = entry.get("question")
        if not isinstance(question, str) or not question:
            continue

        item: dict[str, str | list[str]] = {"question": question}
        answer = entry.get("answer")
        if answer is not None and str(answer).strip():
            item["answer"] = str(answer).strip()
        else:
            choices = entry.get("choices")
            if isinstance(choices, list):
                item["choices"] = [str(choice) for choice in choices]
        items.append(item)

    return items


def _extract_answered_founder_questions(content: str) -> list[dict[str, str]]:
    """Return founder questions that already have captured answers."""
    answered: list[dict[str, str]] = []
    for item in _extract_founder_question_items(content):
        answer = item.get("answer")
        if isinstance(answer, str) and answer:
            answered.append({"question": item["question"], "answer": answer})
    return answered


def _merge_founder_answers(question_items: list[dict], answers: list[dict[str, str]]) -> list[dict]:
    """Apply founder answers to structured question items."""
    answer_map = {
        item["question"]: item["answer"]
        for item in answers
        if isinstance(item.get("question"), str) and isinstance(item.get("answer"), str)
    }
    merged: list[dict] = []
    for item in question_items:
        question = item.get("question")
        if not isinstance(question, str) or not question:
            continue

        updated: dict[str, str | list[str]] = {"question": question}
        if question in answer_map:
            updated["answer"] = answer_map[question]
        elif isinstance(item.get("answer"), str) and item["answer"]:
            updated["answer"] = item["answer"]
        elif isinstance(item.get("choices"), list):
            updated["choices"] = [str(choice) for choice in item["choices"]]
        merged.append(updated)
    return merged


def _replace_founder_questions_block(content: str, updated_questions: list[dict]) -> str:
    """Replace the founder-questions JSON block with updated question data."""
    found = _find_founder_questions_block(content)
    if found is None:
        return content

    data, match = found
    data["founder_questions"] = updated_questions
    replacement = f"```json\n{json.dumps(data, indent=2)}\n```"
    return content[: match.start()] + replacement + content[match.end() :]


def _render_founder_question_section(questions: list[dict], *, heading: str) -> list[str]:
    """Render founder questions or answers as Markdown lines."""
    lines = [heading, ""]
    if not questions:
        lines.append("- None.")
        lines.append("")
        return lines

    for idx, item in enumerate(questions, 1):
        lines.append(f"{idx}. {item.get('question', 'N/A')}")
        answer = item.get("answer")
        if isinstance(answer, str) and answer:
            lines.append(f"   - Answer: {answer}")
            continue

        choices = item.get("choices")
        if isinstance(choices, list) and choices:
            lines.append(f"   - Choices: {json.dumps(choices)}")
        else:
            lines.append("   - Answer: Pending founder input")
    lines.append("")
    return lines


def _replace_prd_open_questions_section(content: str, questions: list[dict]) -> str:
    """Replace the PRD open-questions section with rendered founder decisions."""
    heading_match = re.search(r"^##\s+Open Questions\s*$", content, re.MULTILINE)
    if heading_match is None:
        return content

    section_end = len(content)
    next_heading = re.search(r"^##\s+", content[heading_match.end() :], re.MULTILINE)
    if next_heading is not None:
        section_end = heading_match.end() + next_heading.start()

    found = _find_founder_questions_block(content)
    if found is not None:
        _, match = found
        if match.start() > heading_match.start():
            section_end = min(section_end, match.start())

    replacement = "\n".join(_render_founder_question_section(questions, heading="## Open Questions"))
    prefix = content[: heading_match.start()].rstrip()
    suffix = content[section_end:].lstrip("\n")
    updated = replacement if not prefix else prefix + "\n\n" + replacement
    if suffix:
        updated += "\n\n" + suffix
    return updated


def _apply_founder_answers_to_content(content: str, answers: list[dict[str, str]]) -> str:
    """Update the structured founder question block with captured answers."""
    question_items = _extract_founder_question_items(content)
    if not question_items or not answers:
        return content
    merged = _merge_founder_answers(question_items, answers)
    return _replace_founder_questions_block(content, merged)


def _apply_founder_answers_to_prd(content: str, answers: list[dict[str, str]]) -> str:
    """Apply founder answers to PRD markdown and trailing JSON."""
    updated = _apply_founder_answers_to_content(content, answers)
    return _replace_prd_open_questions_section(updated, _extract_founder_question_items(updated))
