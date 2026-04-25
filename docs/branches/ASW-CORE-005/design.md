# Design Plan: Slice 3 Task-Owner Driven Phase Planning

## Objective

Slice 3 should promote the approved phase task mapping into a first-class orchestration input so later implementation slices run the work that was actually planned, in dependency order, by the approved task owners. The codebase already asks Development Lead to emit a structured `Task Mapping` JSON block inside each phase design, but orchestration currently validates that block and then discards it. The next slice should preserve and expose that structure rather than continuing to treat `selected_team_roles` as the implementation unit.

## Current State

- `_phase_design_request()` already requires a `Task Mapping` JSON block with `id`, `title`, `owner`, `objective`, `depends_on`, `deliverables`, and `acceptance_criteria`.
- `lint_phase_design()` already extracts that JSON block and `validate_phase_task_mapping()` already validates task ids, owners, and dependencies.
- `_run_phase_design_step()` writes draft, feedback, and final Markdown artifacts, but it does not persist the approved task mapping as a separate machine-consumable artifact.
- The broader implementation-loop design still assumes execution will iterate through `selected_team_roles`, which is now too coarse because the approved phase design already contains explicit task ownership and task ordering.
- Slice 2 introduced `.company/artifacts/validation_contract.json` and `.company/artifacts/validation_contract.md`; slice 3 should treat that contract as part of phase planning context.

## Scope of Slice 3

1. Persist a canonical per-phase task-mapping artifact derived from the approved final phase design.
2. Add a helper surface that can load, order, and group phase tasks for later implementation slices.
3. Include the current validation contract in phase-design planning context so task deliverables and acceptance criteria reflect validation or explicit gap updates when behavior changes.
4. Backfill missing task-mapping artifacts locally from an existing final phase design so older runs do not need a fresh LLM phase-design pass just to gain the new artifact.
5. Add focused tests around parsing, persistence, ordering, and the local backfill path.

Slice 3 should not execute tasks yet. It should only make approved task ownership and task order durable and reusable.

## Core Decisions

- The implementation unit becomes the approved task, not the raw role list from `selected_team_roles`.
- Task owners remain role titles already present in `roster.json`; later slices should only run roles that actually own at least one approved task.
- The canonical task-mapping JSON artifact should keep the same shape already used inside the phase-design Markdown block:

```json
{
    "tasks": [
        {
            "id": "task_one",
            "title": "Task title",
            "owner": "Development Lead",
            "objective": "Why this task exists in this phase",
            "depends_on": [],
            "deliverables": ["Concrete output"],
            "acceptance_criteria": ["How the team will know this task is done"]
        }
    ]
}
```

- Slice 3 should not invent a second schema for the derived artifact. The phase-design JSON block and the persisted JSON artifact should stay structurally identical.
- The final phase-design Markdown remains the human review artifact. The canonical task-mapping JSON and readable companion Markdown are derived from that approved design.
- If a final design artifact already exists and only the derived task-mapping artifacts are missing, regenerate them locally instead of rerunning Development Lead.
- The validation contract is planning input, not a new approval gate in this slice. Validation obligations should be expressed through task deliverables and acceptance criteria rather than through a brand-new task schema.

## Recommended Artifact Shape

Add two per-phase artifacts under `.company/artifacts/phases/`:

- Canonical JSON: `<stem>_task_mapping.json`
- Readable companion: `<stem>_task_mapping.md`

Examples for phase 1:

- `.company/artifacts/phases/01_task_mapping.json`
- `.company/artifacts/phases/01_task_mapping.md`

The Markdown companion should summarize:

- ordered tasks
- owner assignments
- dependency edges
- deliverables
- acceptance criteria

The Markdown summary is derived only from the JSON artifact. The JSON artifact is derived only from the approved final phase design.

## Recommended Helper Surface

Add a dedicated helper module rather than growing `orchestrator.py` or `phase_preparation.py` with more parsing and ordering logic.

Recommended module:

- `src/asw/phase_tasks.py`

Recommended functions:

- `lint_phase_task_mapping_json(content: str, *, allowed_roles: set[str] | None = None) -> tuple[list[str], dict | None]`
- `render_phase_task_mapping_markdown(task_mapping: dict, *, phase_label: str) -> str`
- `write_phase_task_mapping(task_mapping: dict, paths: PhaseArtifactPaths, *, phase_label: str) -> None`
- `load_phase_task_mapping(paths: PhaseArtifactPaths) -> dict | None`
- `ordered_phase_tasks(task_mapping: dict) -> list[dict]`
- `tasks_owned_by(task_mapping: dict, owner: str) -> list[dict]`

Recommended path additions to `PhaseArtifactPaths`:

- `task_mapping_json_path`
- `task_mapping_md_path`

## Orchestrator Changes

### 1. Phase-design prompt and context

Update the phase-design prompt so the Development Lead sees the current validation contract during planning.

The prompt should explicitly instruct:

- keep task ownership explicit
- keep dependency order explicit
- keep task ids stable across harmonization
- when a task changes product behavior, capture the required validation coverage or explicit known-gap update in the task deliverables or acceptance criteria

`_run_phase_design_step()` should include the current validation contract in the Development Lead context, preferably using the canonical JSON artifact and optionally the readable Markdown companion.

### 2. Persist approved task mapping after final design

After the final phase design passes linting, extract the approved JSON task-mapping block and write the two new derived artifacts locally.

That means `_run_phase_design_step()` should do three things after writing the final design:

1. extract the `Task Mapping` JSON block from the final design
2. validate and parse it into canonical JSON
3. write `<stem>_task_mapping.json` and `<stem>_task_mapping.md`

### 3. Track derived artifacts in phase-design state

The phase-design output set should now include the two task-mapping artifacts in addition to the draft, feedback, and final Markdown files.

However, slice 3 should avoid forcing a new LLM run when the final design is already present and only the derived task-mapping artifacts are missing.

Recommended handling:

- before deciding a saved phase design is stale, try to backfill missing task-mapping artifacts from the existing final design locally
- only rerun the Development Lead phase-design step when the final design itself is missing or when tracked inputs have genuinely changed

This keeps the migration from slice 2 to slice 3 cheap and deterministic.

### 4. Prepare the seam for later implementation slices

Slice 3 does not run implementation, but it should give slice 4 a clean entry point.

Later slices should be able to replace logic like:

- iterate through `team_entries = _phase_team_entries(roster_json, phase_data)`

with logic closer to:

- load task mapping for the phase
- order tasks by dependency
- hand later slices the owner-specific task list and the roster entry for each owner

`selected_team_roles` should remain important for phase design participation and feedback collection, but not as the future execution loop's primary unit of work.

## Suggested Code Locations

- `src/asw/phase_tasks.py`: canonical task-mapping helpers, persistence, ordering, and rendering
- `src/asw/phase_preparation.py`: keep the Markdown-section extraction helper and `PhaseArtifactPaths`; extend paths for task-mapping artifacts
- `src/asw/orchestrator.py`: pass validation-contract context into phase design, persist derived task-mapping artifacts, and backfill them on skip paths
- `tests/test_phase_tasks.py`: new focused tests for parsing, persistence, owner grouping, and dependency ordering
- `tests/test_orchestrator.py`: narrow regression proving backfill from an existing final design does not require a new LLM call

## Relationship to Slice 2

Slice 2 established the project-wide validation contract as a first-class artifact. Slice 3 should treat that contract as part of phase delivery planning.

Concretely:

- phase-design generation should read the current validation contract
- task planning should account for validation additions or explicit coverage gaps when behavior changes
- slice 3 should not execute validations yet

This keeps planning aligned with the current regression boundary before slice 4 starts implementation execution.

## Non-Goals for Slice 3

- Do not implement the per-task Plan -> Execute loop yet.
- Do not add Development Lead approval parsing for implementation diffs yet.
- Do not change the execution-plan or roster schema.
- Do not replace the final phase-design Markdown as the human review artifact.
- Do not add validation evidence logs yet.

## Risks and Mitigations

- Risk: task mapping drifts from the final design.
    Mitigation: derive task-mapping artifacts from the approved final design; never edit the Markdown companion directly.

- Risk: dependency cycles only show up once implementation starts.
    Mitigation: add explicit topological ordering with cycle detection in the new helper module.

- Risk: slice 3 forces unnecessary phase-design reruns for existing runs.
    Mitigation: backfill derived artifacts locally from an existing final design before invalidating the saved phase state.

- Risk: later slices still execute by role instead of by approved task.
    Mitigation: make the new task-loading and ordering helpers the only supported future seam for implementation orchestration.

- Risk: validation contract is ignored during planning.
    Mitigation: add it to Development Lead planning context and require validation obligations to appear in task deliverables or acceptance criteria.

## Verification Plan for Slice 3

- Unit test valid task-mapping parse and load helpers.
- Unit test Markdown rendering for the derived task-mapping summary.
- Unit test dependency ordering and cycle rejection.
- Unit test owner filtering or grouping helpers.
- Narrow orchestrator test that an existing final phase design can backfill missing task-mapping artifacts locally without another LLM phase-design call.
- Narrow orchestrator test that the validation contract is included in the phase-design context for Development Lead.

## Recommended Starting Point for the Next Session

1. add `src/asw/phase_tasks.py` and `tests/test_phase_tasks.py`
2. extend `PhaseArtifactPaths` with task-mapping artifact paths
3. wire local extraction and persistence after final phase design
4. add the local backfill path for saved phase designs missing only the new derived artifacts
5. only after that, let later slices consume ordered tasks instead of `selected_team_roles`
