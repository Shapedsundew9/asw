# Execution Plan Template

Use this as a structural guide for the VP Engineering execution plan JSON.

Required top-level keys:

- `phases`
- `selected_team`
- `generic_role_catalog`
- `deferred_roles_or_capabilities`
- optional `founder_questions`

Recommended shape:

```json
{
  "phases": [
    {
      "id": "phase_1",
      "name": "Phase name",
      "objective": "What this phase proves or delivers.",
      "scope": "Explicit in-scope and out-of-scope boundary.",
      "deliverables": ["Deliverable 1"],
      "exit_criteria": ["Criterion 1"],
      "selected_team_roles": ["Role title"]
    }
  ],
  "selected_team": [
    {
      "title": "Role title",
      "filename": "role_title.md",
      "responsibility": "One-sentence ownership summary.",
      "rationale": "Why this role is needed now."
    }
  ],
  "generic_role_catalog": [
    {
      "title": "Generic role title",
      "summary": "General description of the role.",
      "when_needed": "Conditions under which this role becomes justified."
    }
  ],
  "deferred_roles_or_capabilities": [
    {
      "name": "Deferred role or capability",
      "rationale": "Why it can wait until a later phase."
    }
  ],
  "founder_questions": [
    {
      "question": "Optional unresolved decision",
      "choices": ["Choice A", "Choice B"]
    }
  ]
}
```
