# ASW-CORE-004 Change Log

## Branch Summary

- Starts the V0.4 implementation with the smallest safe slice instead of the full phase loop.
- Makes `Development Lead` and `DevOps Engineer` mandatory core roles in the founder-approved execution plan and Hiring Manager roster.
- Adds bundled role prompts for those two immutable project roles while deferring the full phase-loop orchestration.

## Phase 1 Scope

### Added

- Shared core-role definitions for mandatory project roles.
- Bundled role prompts for `development_lead.md` and `devops_engineer.md`.

### Changed

- VP Engineering guidance and execution-plan validation now require both core roles in `selected_team`.
- Execution plans now require both core roles in each phase's `selected_team_roles`.
- Hiring roster validation now fails when either core role is missing.
- Role generation uses bundled immutable core role files instead of regenerating them through the Role Writer.

## Deferred

- Phase design artifacts such as `phase_{N}_design_draft.md` and `phase_{N}_design_final.md`.
- DevOps setup-script generation and execution.
- Sequential implementation task loops.
- Development Lead delta-review JSON and rework loops.
- Sub-phase resume tracking in `pipeline_state.json`.
