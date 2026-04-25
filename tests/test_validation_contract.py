"""Tests for validation-contract helpers."""

from __future__ import annotations

import json
from pathlib import Path

from asw.company import init_company
from asw.linters.json_lint import validate_validation_contract
from asw.validation_contract import (
    ensure_validation_contract,
    lint_validation_contract_json,
    load_validation_contract,
    new_validation_contract,
    render_validation_contract_markdown,
    validation_contract_paths,
    write_validation_contract,
)

_VALID_CONTRACT = {
    "version": "1.0",
    "owner": "Development Lead",
    "summary": "Covers the core product validation checks.",
    "validations": [
        {
            "id": "unit_tests",
            "title": "Unit tests",
            "kind": "command",
            "command": "python -m pytest",
            "working_directory": ".",
            "success_criteria": ["Pytest exits with code 0."],
            "protects": ["Core CLI flow"],
            "always_run": True,
            "enabled": True,
        }
    ],
    "protected_behaviors": ["Core CLI flow"],
    "known_gaps": ["No end-to-end coverage yet."],
    "change_policy": "Any new or changed behavior must either add validation coverage or record an explicit known gap.",
}


def test_new_validation_contract_bootstraps_known_gaps() -> None:
    """Default contracts should start with explicit known gaps and no validations."""
    contract = new_validation_contract()

    assert contract["owner"] == "Development Lead"
    assert not contract["validations"]
    assert not contract["protected_behaviors"]
    assert contract["known_gaps"]


def test_validate_validation_contract_accepts_valid_contract() -> None:
    """A fully populated validation contract should pass mechanical validation."""
    errors = validate_validation_contract(json.dumps(_VALID_CONTRACT))
    lint_errors, contract = lint_validation_contract_json(json.dumps(_VALID_CONTRACT))

    assert not errors
    assert not lint_errors
    assert contract == _VALID_CONTRACT


def test_validate_validation_contract_rejects_command_without_command() -> None:
    """Command validations must include a command to execute."""
    payload = json.loads(json.dumps(_VALID_CONTRACT))
    del payload["validations"][0]["command"]

    errors = validate_validation_contract(json.dumps(payload))

    assert any("command" in error for error in errors)


def test_validate_validation_contract_rejects_unsupported_kind() -> None:
    """Validation entries must declare a supported validation kind."""
    payload = json.loads(json.dumps(_VALID_CONTRACT))
    payload["validations"][0]["kind"] = "script"

    errors = validate_validation_contract(json.dumps(payload))

    assert any("kind" in error for error in errors)


def test_render_validation_contract_markdown_includes_expected_sections() -> None:
    """Rendered Markdown should surface the contract summary, checks, and gaps."""
    markdown = render_validation_contract_markdown(_VALID_CONTRACT)

    assert "# Validation Contract" in markdown
    assert "## Active Validations" in markdown
    assert "Unit tests" in markdown
    assert "Core CLI flow" in markdown
    assert "No end-to-end coverage yet." in markdown
    assert "## Change Policy" in markdown


def test_write_and_load_validation_contract_round_trip(tmp_path: Path) -> None:
    """Validation contract helpers should persist and load canonical contract artifacts."""
    company = init_company(tmp_path)

    write_validation_contract(_VALID_CONTRACT, company)

    contract_json_path, contract_md_path = validation_contract_paths(company)
    assert contract_json_path.is_file()
    assert contract_md_path.is_file()
    assert load_validation_contract(company) == _VALID_CONTRACT


def test_ensure_validation_contract_preserves_existing_contract(tmp_path: Path) -> None:
    """Bootstrap should not overwrite an existing valid contract on rerun."""
    company = init_company(tmp_path)
    write_validation_contract(_VALID_CONTRACT, company)

    ensured = ensure_validation_contract(company)

    assert ensured == _VALID_CONTRACT
    assert load_validation_contract(company) == _VALID_CONTRACT
