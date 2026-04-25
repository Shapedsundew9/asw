"""Validation-contract artifact helpers."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from asw.linters.json_lint import validate_validation_contract

logger = logging.getLogger("asw.validation_contract")

_VALIDATION_CONTRACT_JSON = "validation_contract.json"
_VALIDATION_CONTRACT_MARKDOWN = "validation_contract.md"
_DEFAULT_CHANGE_POLICY = (
    "Any new or changed behavior must either add validation coverage or record an explicit known gap."
)


def validation_contract_paths(company: Path) -> tuple[Path, Path]:
    """Return the canonical JSON and Markdown paths for the validation contract."""
    artifacts_dir = company / "artifacts"
    return artifacts_dir / _VALIDATION_CONTRACT_JSON, artifacts_dir / _VALIDATION_CONTRACT_MARKDOWN


def new_validation_contract() -> dict:
    """Return the default validation contract for a newly initialised company."""
    return {
        "version": "1.0",
        "owner": "Development Lead",
        "summary": "Validation coverage is bootstrapped and must grow with the product.",
        "validations": [],
        "protected_behaviors": [],
        "known_gaps": [
            "No automated validations are defined yet.",
            "Deferred or manual validation coverage has not been curated yet.",
        ],
        "change_policy": _DEFAULT_CHANGE_POLICY,
    }


def lint_validation_contract_json(content: str) -> tuple[list[str], dict | None]:
    """Return validation errors and parsed JSON for canonical contract content."""
    errors = validate_validation_contract(content)
    if errors:
        return errors, None

    data = json.loads(content)
    if not isinstance(data, dict):
        return [f"Expected a JSON object at top level, got {type(data).__name__}."], None
    return [], data


def render_validation_contract_markdown(contract: dict) -> str:
    """Render a human-readable Markdown companion for the validation contract."""
    lines = [
        "# Validation Contract",
        "",
        "> **Source of Truth:** The validation contract is stored in `validation_contract.json`.",
        "",
        "## Validation Summary",
        f"- **Owner:** {contract.get('owner', 'N/A')}",
        f"- **Version:** {contract.get('version', 'N/A')}",
        f"- **Summary:** {contract.get('summary', 'N/A')}",
        "",
        "## Active Validations",
    ]

    active_validations = [
        entry for entry in contract.get("validations", []) if isinstance(entry, dict) and entry.get("enabled", True)
    ]
    if active_validations:
        for entry in active_validations:
            lines.extend(
                [
                    f"### {entry.get('title', 'N/A')} (`{entry.get('id', 'N/A')}`)",
                    f"- **Kind:** {entry.get('kind', 'N/A')}",
                    f"- **Always Run:** {entry.get('always_run', False)}",
                ]
            )
            working_directory = entry.get("working_directory")
            if isinstance(working_directory, str) and working_directory.strip():
                lines.append(f"- **Working Directory:** {working_directory}")
            command = entry.get("command")
            if isinstance(command, str) and command.strip():
                lines.append(f"- **Command:** `{command}`")

            lines.append("- **Success Criteria:**")
            for item in entry.get("success_criteria", []):
                lines.append(f"  - {item}")

            lines.append("- **Protects:**")
            for item in entry.get("protects", []):
                lines.append(f"  - {item}")
            lines.append("")
    else:
        lines.append("- None yet.")
        lines.append("")

    lines.append("## Protected Behaviors")
    protected_behaviors = contract.get("protected_behaviors", [])
    if isinstance(protected_behaviors, list) and protected_behaviors:
        lines.extend(f"- {item}" for item in protected_behaviors)
    else:
        lines.append("- None documented yet.")

    lines.extend(["", "## Known Gaps"])
    known_gaps = contract.get("known_gaps", [])
    if isinstance(known_gaps, list) and known_gaps:
        lines.extend(f"- {item}" for item in known_gaps)
    else:
        lines.append("- None documented.")

    lines.extend(
        [
            "",
            "## Change Policy",
            contract.get("change_policy", "N/A"),
            "",
        ]
    )
    return "\n".join(lines)


def load_validation_contract(company: Path) -> dict | None:
    """Load and validate the canonical validation contract, if present."""
    contract_json_path, _ = validation_contract_paths(company)
    if not contract_json_path.is_file():
        return None

    try:
        content = contract_json_path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("Could not read validation contract: %s", contract_json_path)
        return None

    errors, contract = lint_validation_contract_json(content)
    if errors:
        logger.warning("Validation contract is invalid at %s: %s", contract_json_path, "; ".join(errors))
        return None
    return contract


def write_validation_contract(contract: dict, company: Path) -> None:
    """Write the canonical JSON validation contract and derived Markdown summary."""
    contract_json = json.dumps(contract, indent=2) + "\n"
    errors, validated_contract = lint_validation_contract_json(contract_json)
    if errors or validated_contract is None:
        raise ValueError(f"Invalid validation contract: {'; '.join(errors)}")

    contract_json_path, contract_md_path = validation_contract_paths(company)
    contract_json_path.parent.mkdir(parents=True, exist_ok=True)
    contract_json_path.write_text(json.dumps(validated_contract, indent=2) + "\n", encoding="utf-8")
    contract_md_path.write_text(render_validation_contract_markdown(validated_contract), encoding="utf-8")


def ensure_validation_contract(company: Path) -> dict:
    """Ensure the canonical validation contract exists and return the parsed contract."""
    contract_json_path, contract_md_path = validation_contract_paths(company)
    contract = load_validation_contract(company)
    if contract is not None:
        if not contract_md_path.is_file():
            contract_md_path.write_text(render_validation_contract_markdown(contract), encoding="utf-8")
        return contract

    if contract_json_path.exists():
        raise ValueError(f"Existing validation contract is invalid: {contract_json_path}")

    contract = new_validation_contract()
    write_validation_contract(contract, company)
    logger.info("Validation contract bootstrapped: %s", contract_json_path)
    return contract
