# Quickstart

Run the current pipeline once, review the planning gates, and inspect the main artifacts it leaves behind.

## Before You Start

Complete [Installation](installation.md) first. Use an interactive terminal, and make sure Gemini works in the same shell session:

```bash
env | grep GEMINI_API_KEY
gemini -p "Reply with OK" -o json
```

## Step 1 - Create A Project Folder

```bash
mkdir my-first-project
cd my-first-project
git init
git commit --allow-empty -m "Initial commit"
```

If you only want to experiment, you can skip the git setup and add `--no-commit` in Step 3.

## Step 2 - Write A Vision Document

Create `vision.md` with a short, plain-English description of the product you want the simulated company to build.

```markdown
# Vision: Task Tracker CLI

## Product Overview
A command-line task tracker that lets users add, list, complete, and
delete tasks. All data is stored in a local JSON file.

## Target Audience
Individual developers and power users who prefer terminal-based tools.

## Core Requirements
- Add a task with a title and an optional due date.
- List all tasks, optionally filtered by status.
- Mark a task as complete.
- Delete a task by ID.
- Persist all data in ~/.tasks.json.

## Definition of Done
A working CLI with unit tests and a README.
```

There is no strict template. The important part is that the vision is clear enough for PRD, architecture, and execution planning to stay aligned.

## Step 3 - Start The Pipeline

```bash
asw start --vision vision.md
```

If you want a debug log for the run:

```bash
asw start --vision vision.md --debug
```

The startup output begins like this:

```text
========================================================================
  AgenticOrg CLI – V0.3 Pipeline
========================================================================
✓ Vision loaded: vision.md (604 chars)
✓ LLM backend: Gemini CLI

✓ Company directory initialised: /path/to/my-first-project/.company
```

At this point `asw` has also bootstrapped the validation-contract files under `.company/artifacts/`.

## Step 4 - Review The Three Planning Gates

The first interactive part of the run is the planning sequence:

1. PRD
2. Architecture
3. Execution Plan

Each of those phases can ask structured founder questions before the review menu appears. Your answers are written back into the artifact locally, then `asw` shows the updated artifact for review.

The review menu supports these actions:

| Choice | What Happens |
|--------|--------------|
| Approve | Accept the artifact and continue |
| Reject | Discard the current draft and rerun the phase from its original context |
| Modify | Provide notes and rerun with your guidance |
| Request More Questions | Ask for another question round focused on unresolved issues |
| Stop | Exit cleanly with everything created so far left on disk |

When you use **Modify**, press `Esc`, then `Enter`, to submit the multiline feedback prompt.

For the execution-plan gate, **Modify** also accepts a full JSON object. If you paste edited execution-plan JSON, `asw` validates it locally before continuing.

## Step 5 - Inspect The Core Artifacts

After the execution plan is approved, `asw` automatically generates the roster, role prompts, and validation-contract companion files.

Inspect the artifacts directory:

```bash
ls .company/artifacts
sed -n '1,200p' .company/artifacts/validation_contract.md
```

Important files at this stage include:

- `prd.md`
- `architecture.json`
- `architecture.md`
- `execution_plan.json`
- `execution_plan.md`
- `roster.json`
- `roster.md`
- `validation_contract.json`
- `validation_contract.md`

The validation contract starts small by default. It usually contains an empty `validations` array and a change policy that tells later phases to add coverage or record explicit known gaps when behavior changes.

## Step 6 - Inspect The Phase-Preparation Artifacts

After the planning artifacts are complete, `asw` iterates through each approved execution-plan phase and prepares the implementation work.

Inspect the first phase:

```bash
ls .company/artifacts/phases
sed -n '1,220p' .company/artifacts/phases/01_design_final.md
sed -n '1,220p' .company/artifacts/phases/01_task_mapping.md
```

For each phase, `asw` writes artifacts such as:

- `01_design_draft.md`
- `01_feedback_*.md`
- `01_design_final.md`
- `01_task_mapping.json`
- `01_task_mapping.md`
- `01_setup_proposal.md`
- `01_setup_summary.md`

It also extracts a setup script into the workspace DevContainer directory:

```bash
ls .devcontainer
```

The script path follows this pattern:

- `.devcontainer/phase_01_setup.sh`

By default, `asw` records setup execution as deferred. It writes the proposal, summary, and script, but does not run the script.

If you explicitly want the dangerous setup-execution path, rerun with:

```bash
asw start --vision vision.md --execute-phase-setups
```

That opt-in path adds a separate founder execution gate before any generated setup script runs.

## Step 7 - Inspect The Implementation-Turn Artifacts

Once phase preparation is complete, `asw` executes implementation turns. Each turn groups the ready tasks owned by one role, then records plan, execute, validation, review, and commit evidence.

Inspect the first turn artifacts:

```bash
ls .company/artifacts/phases
sed -n '1,220p' .company/artifacts/phases/01_turn_01_*_validation.md
sed -n '1,220p' .company/artifacts/phases/01_turn_01_*_review.md
```

Each turn writes files that follow this pattern:

- `01_turn_01_<role>_attempt_1_plan.md`
- `01_turn_01_<role>_attempt_1_execute.md`
- `01_turn_01_<role>_attempt_1_validation.md`
- `01_turn_01_<role>_attempt_1_scope.md`
- `01_turn_01_<role>_attempt_1_review.md`
- `01_turn_01_<role>_attempt_1_commit.md`

The validation step reruns the current validation contract after execution. If the validation report fails or the Development Lead review requests revisions, `asw` retries the same turn with concrete follow-up guidance. If retries run out, the run stops and leaves all artifacts on disk for inspection.

## Step 8 - Understand The Resulting Workspace

After a successful run, your project will look roughly like this:

```text
my-first-project/
  vision.md
  .company/
    pipeline_state.json
    roles/
      cpo.md
      cto.md
      vpe.md
      hiring_manager.md
      role_writer.md
      development_lead.md
      phase_feedback_reviewer.md
      devops_engineer.md
      <generated-role>.md
    artifacts/
      prd.md
      architecture.json
      architecture.md
      execution_plan.json
      execution_plan.md
      roster.json
      roster.md
      validation_contract.json
      validation_contract.md
      phases/
        01_design_draft.md
        01_feedback_*.md
        01_design_final.md
        01_task_mapping.json
        01_task_mapping.md
        01_setup_proposal.md
        01_setup_summary.md
        01_turn_*_plan.md
        01_turn_*_execute.md
        01_turn_*_validation.md
        01_turn_*_scope.md
        01_turn_*_review.md
        01_turn_*_commit.md
    memory/
    templates/
    standards/
  .devcontainer/
    phase_01_setup.sh
```

If commits are enabled, your git history will include messages like:

```text
[asw] Phase: prd-generation completed
[asw] Phase: architecture-generation completed
[asw] Phase: execution-plan-generation completed
[asw] Phase: hiring completed
[asw] Phase: phase_1:turn:1 completed
```

By default, those commits stage `.company/` plus any approved implementation-turn paths. If you run with `--stage-all`, `asw` stages the full worktree instead.

## Step 9 - Run It Again Safely

Rerunning the same command resumes from saved state:

```bash
asw start --vision vision.md
```

`asw` compares tracked input and output hashes in `.company/pipeline_state.json`.

- If a completed step is still current, `asw` skips it.
- If outputs are missing or changed, `asw` reruns the step.
- If tracked inputs changed but saved outputs still exist, `asw` prompts you to continue, rerun, or restart.

Use `--restart` when you want to delete `.company/` and rebuild from scratch.

## Troubleshooting

- If Gemini authentication fails, re-check `GEMINI_API_KEY` and rerun `gemini -p "Reply with OK" -o json` before retrying `asw`.
- If the working directory is not a git repository, either initialize git or rerun with `--no-commit`.
- If a generated artifact fails structural validation, inspect `.company/artifacts/failed/`.
- If an implementation turn stops, inspect the latest `*_validation.md` and `*_review.md` files under `.company/artifacts/phases/`.
- If you need a detailed execution trace, rerun with `--debug`.

## What's Next

- [First Complete Run](../tutorials/first-project.md) - a deeper walkthrough with commentary on each stage
- [Key Concepts](../reference/concepts.md) - understand how the loops and gates fit together
- [Runs, State, and Recovery](../reference/runs-and-state.md) - learn how resume, invalidation, and restart behavior work in detail
