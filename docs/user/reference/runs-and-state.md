# Runs, State, and Recovery

This reference explains how `asw` resumes runs, reacts to changed vision files, and helps you recover from failures or stale artifacts.

## What `asw` Stores Between Runs

Every run uses a `.company/` directory inside your working directory. One file in that directory controls resume behavior:

```text
.company/
  pipeline_state.json
```

`pipeline_state.json` records:

- The pipeline version.
- A SHA-256 hash of the vision file used for the last run.
- Which phases completed successfully.

`asw` checks that saved state against the artifacts on disk before deciding whether to skip or rerun a phase.

## How Resume Works

When you rerun the same command, `asw` does not blindly start over. It compares saved state, the current vision file, and the expected artifact files.

```mermaid
%%{init: {'theme': 'dark', 'themeVariables': {'primaryColor': '#2d6a4f', 'primaryTextColor': '#d8f3dc', 'primaryBorderColor': '#52b788', 'lineColor': '#74c69d', 'secondaryColor': '#1b4332', 'tertiaryColor': '#40916c', 'edgeLabelBackground': '#1b4332'}}}%%
flowchart TD
    A[Run asw start] --> B{pipeline_state.json exists?}
    B -- No --> C[Run all phases]
    B -- Yes --> D{Vision hash changed?}
    D -- Yes --> E[Choose Continue or Restart]
    D -- No --> F{Phase marked complete and artifacts still exist?}
    E -- Continue --> F
    E -- Restart --> G[Delete .company and start fresh]
    F -- Yes --> H[Skip completed phase]
    F -- No --> I[Run phase again]
    C --> J[Advance through remaining phases]
    G --> J
    H --> J
    I --> J
```

In practice this means:

- If PRD, architecture, execution-plan, and roster artifacts still exist, those completed phases can be skipped on a later run.
- For PRD, architecture, execution plan, and roster, if a phase is marked complete but one of its required artifacts is missing, `asw` reruns that phase and the phases after it.
- Role generation is skipped only if the generated role files implied by the approved roster still exist on disk.
- Resume works even when you used `--no-commit`; saved state is separate from git.

## What Happens When The Vision Changes

If the vision file contents change after a previous run, `asw` detects the new hash and asks whether to continue from the saved state or restart from scratch.

Use **Continue** when your edit is small and the existing artifacts are still acceptable.

Use **Restart** when the product scope, target users, technical assumptions, or execution plan changed enough that the saved PRD or architecture is no longer trustworthy.

## Force A Clean Restart

Use `--restart` when you know the existing `.company/` directory should be discarded:

```bash
asw start --vision vision.md --restart
```

This deletes `.company/` before the run starts and then rebuilds it from the bundled roles, templates, and standards.

Common reasons to use `--restart`:

- You want a completely fresh PRD and architecture.
- You want a fresh execution plan and first-phase team recommendation.
- You significantly rewrote the vision file.
- You manually edited artifacts and want to discard those edits.
- You suspect saved state and on-disk artifacts are out of sync.

## Continue After A Partial Run

If you stop at a Founder Review Gate or the run exits partway through, rerun the same command:

```bash
asw start --vision vision.md
```

`asw` resumes from the first incomplete phase that still needs work.

Examples:

- If PRD and architecture were already approved, the rerun starts at the execution-plan phase.
- If `execution_plan.json` was deleted after a previous run, the execution-plan phase runs again.
- If `roster.json` was deleted after a previous run, the roster phase runs again.
- If a generated role file was deleted after a previous run, the roles phase runs again.
- If all reviewable artifacts and expected generated role files still exist, the rerun quickly skips everything.

## When Template And Standards Edits Apply

Resume decisions are artifact-based, not template-diff-based. If you edit
files under `.company/templates/` or `.company/standards/`, `asw` does not
automatically invalidate completed phases.

In practice:

- Editing `.company/templates/execution_plan_template.md` affects the VP
  Engineering phase only the next time the execution plan is regenerated.
- Editing `.company/templates/role_template.md` or the contents of an already
  assigned standards file affects generated specialist role prompts only the
  next time role generation runs.
- Adding, removing, or renaming standards files affects the Hiring Manager
  phase because roster generation sees the current list of available
  standards filenames.

To make those edits take effect, either use `--restart` for a clean rebuild
or rerun the relevant downstream phase by removing its generated artifacts
before starting again.

Useful examples:

- Remove `execution_plan.json` or `execution_plan.md` if you want the
  execution-plan phase and all later phases to rerun.
- Remove `roster.json` or `roster.md` if you changed the available standards
  filenames and want the Hiring Manager to elaborate briefs again.
- Remove one generated specialist role file listed in `roster.json` if you
  only want role generation to run again with the current approved roster.

## Create Debug Logs

Use `--debug` to capture detailed logs from the CLI, orchestrator, and backend.

Create a timestamped log file in the current directory:

```bash
asw start --vision vision.md --debug
```

Write logs to an explicit path:

```bash
asw start --vision vision.md --debug asw.log
```

If you want to use a nested path such as `logs/asw.log`, create the parent directory first.

Debug logs are useful when:

- Gemini retries due to timeouts, rate limits, or other transient failures.
- A generated artifact fails mechanical linting.
- You want a record of the raw artifact text and phase transitions.

The debug log now keeps one canonical combined prompt entry per Gemini invocation rather than repeating the same role and prompt content across multiple logging layers.

If you omit the log path, `asw` creates a file named like `asw-debug-YYYYMMDD-HHMMSS.log` in the directory where you ran the command.

## Recovery Tips

- If a run fails on a missing git repository, either initialize git or rerun with `--no-commit`.
- If a generated artifact is structurally invalid, inspect the saved file under `.company/artifacts/failed/`, then update the vision or your custom role/template files and rerun.
- If the saved artifacts look stale after major edits, use `--restart` instead of trying to repair the state manually.
- If you need to inspect what happened before the failure, rerun with `--debug` and keep the log file.

## See Also

- [CLI Reference](cli.md) - command syntax, flags, and exit codes
- [Key Concepts](concepts.md) - pipeline phases, review gates, and `.company/`
- [Quickstart](../getting-started/quickstart.md) - a practical first run
