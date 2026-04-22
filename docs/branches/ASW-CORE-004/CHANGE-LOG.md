# ASW-CORE-004 Change Log

## Branch Summary

- Starts the V0.4 implementation with the smallest safe slice instead of the full phase loop.
- Makes `Development Lead` and `DevOps Engineer` mandatory core roles in the founder-approved execution plan and Hiring Manager roster.
- Adds the first executable phase-preparation slice: Development Lead design artifacts, per-role planning feedback, DevOps setup proposals, and a hard Founder gate before any DevOps script executes in the self-hosted repo.

## Phase 1 Scope

### Added

- Shared core-role definitions for mandatory project roles.
- Bundled role prompts for `development_lead.md` and `devops_engineer.md`.
- A bundled `phase_feedback_reviewer.md` role used to collect per-role feedback on draft phase designs.
- `.company/artifacts/phases/` artifacts for design drafts, per-role feedback, harmonized design, setup proposals, setup summaries, and setup attempt logs.
- Generated per-phase DevOps setup scripts under `.devcontainer/phase_{N}_setup.sh`.
- Guarded phase-preparation helpers and safety validators for task mapping, tooling lists, and DevOps setup proposals.

### Changed

- VP Engineering guidance and execution-plan validation now require both core roles in `selected_team`.
- Execution plans now require both core roles in each phase's `selected_team_roles`.
- Hiring roster validation now fails when either core role is missing.
- Role generation uses bundled immutable core role files instead of regenerating them through the Role Writer.
- The orchestrator now iterates approved execution-plan phases after hiring and runs a design-elaboration loop before any implementation work exists.
- DevOps setup is now split into proposal generation and execution. Proposal generation is automated, but script execution is blocked behind an explicit Founder approval gate.
- `pipeline_state.json` now records namespaced phase-loop steps such as `phase-loop:phase_1:design`, `phase-loop:phase_1:devops-proposal`, and `phase-loop:phase_1:devops-execution`.
- DevOps execution snapshots tracked repo files before and after the script runs and fails the sub-phase if tracked files mutate outside the approved artifact boundary.

## Deferred

- Sequential implementation task loops.
- Development Lead delta-review JSON and rework loops.
- Founder approval and delta loops for per-task implementation changes after environment prep.
- Full task-level resume tracking within sequential implementation.
