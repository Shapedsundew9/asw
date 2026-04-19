import json
from asw.orchestrator import _extract_founder_questions

def test_extract_questions_no_json():
    content = "# PRD\nNo questions here."
    assert _extract_founder_questions(content) is None

def test_extract_questions_wrong_json():
    content = "# PRD\n```json\n{\"foo\": \"bar\"}\n```"
    assert _extract_founder_questions(content) is None

def test_extract_questions_success():
    questions = [{"question": "Database?", "choices": ["PG", "Lite"]}]
    content = f"# PRD\n```json\n{json.dumps({'founder_questions': questions})}\n```"
    assert _extract_founder_questions(content) == questions

def test_extract_questions_multiple_blocks():
    # Should find the one with founder_questions
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

def test_extract_questions_permissive():
    # Test without newline after ```json
    questions = [{"question": "Database?"}]
    content = f"```json {json.dumps({'founder_questions': questions})}```"
    # This currently fails because of \n in regex
    assert _extract_founder_questions(content) == questions

def test_extract_questions_uppercase():
    questions = [{"question": "Database?"}]
    content = f"```JSON\n{json.dumps({'founder_questions': questions})}\n```"
    # This currently fails because of case sensitivity
    assert _extract_founder_questions(content) == questions
