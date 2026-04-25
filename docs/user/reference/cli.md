# CLI Reference

This page documents the current `asw` command surface, supported flags, examples, and exit behavior.

## Global Synopsis

```bash
asw [-h] <command> ...
```

`asw` currently exposes one subcommand: `start`.

Use `asw <command> --help` to inspect command-specific flags.

## `asw start`

Start the pipeline from a vision document.

```bash
asw start [-h] --vision VISION [--workdir WORKDIR] [--no-commit] [--stage-all] [--restart] [--execute-phase-setups] [--debug [LOGFILE]]
```

### Flags

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--vision VISION` | Yes | none | Path to the vision Markdown file. Relative and absolute paths both work. |
| `--workdir WORKDIR` | No | current directory | Working directory where `.company/` is created and where git operations and validation commands run. |
| `--no-commit` | No | off | Skip all git commits. This also removes the git-repository requirement. |
| `--stage-all` | No | off | Stage the full git worktree during automatic commits instead of limiting staging to `.company/` and approved turn paths. |
| `--restart` | No | off | Delete the existing `.company/` directory before starting the run. |
| `--execute-phase-setups` | No | off | Opt into running generated `.devcontainer/phase_<N>_setup.sh` scripts after explicit founder approval. Without this flag, setup proposals and scripts are still generated but execution is recorded as deferred. |
| `--debug [LOGFILE]` | No | off | Enable debug logging. If you omit `LOGFILE`, `asw` creates a timestamped log file in the current directory. If you pass a custom path, `asw` creates missing parent directories automatically. |

### Flag Notes

- `--workdir` controls where `.company/` lives. It also controls the directory used for git checks, automatic commits, and validation-command execution.
- `--stage-all` affects both major phase commits and implementation-turn commits. In implementation turns it intentionally bypasses the normal approved-path-only commit scope.
- `--execute-phase-setups` is the dangerous execution path. Most runs leave it off and inspect the generated setup artifacts without executing them.
- `--debug` only controls logging. It does not change retry behavior, gating, or state tracking.

### Examples

Run in the current directory using a local vision file:

```bash
asw start --vision vision.md
```

Run against a separate working directory:

```bash
asw start --vision ~/ideas/saas-tool.md --workdir ~/projects/saas-tool
```

Run without git commits:

```bash
asw start --vision vision.md --no-commit
```

Stage the full repository during automatic commits:

```bash
asw start --vision vision.md --stage-all
```

Write debug logs to an automatically named file:

```bash
asw start --vision vision.md --debug
```

Write debug logs to a specific file:

```bash
asw start --vision vision.md --debug logs/asw.log
```

Discard the existing `.company/` state and rebuild from scratch:

```bash
asw start --vision vision.md --restart
```

Opt into founder-approved setup-script execution:

```bash
asw start --vision vision.md --execute-phase-setups
```

## Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Pipeline completed successfully, or the Founder stopped the pipeline at a review gate |
| `1` | Startup validation failed, git commit failed, a generated artifact failed structural validation, a setup execution failed, or an implementation turn exhausted its retries |

## What The Command Does

At a high level, `asw start`:

1. Validates startup requirements and reads the vision file
2. Initializes `.company/` and bootstraps the validation contract
3. Runs the PRD, architecture, and execution-plan founder gates
4. Generates the roster and role prompts
5. Prepares each execution-plan phase with design, task-mapping, and setup artifacts
6. Runs implementation turns with validation and Development Lead review

The CLI banner for this flow is currently:

```text
AgenticOrg CLI – V0.3 Pipeline
```

## Environment Requirements

- `gemini` must be installed and available on `PATH`.
- `GEMINI_API_KEY` must be exported in the same shell session that runs `asw`.
- Use an interactive terminal because founder review and optional setup execution are menu-driven.
- The working directory must be inside a git repository unless you pass `--no-commit`.

Quick verification:

```bash
env | grep GEMINI_API_KEY
gemini -p "Reply with OK" -o json
```

## Re-Runs And Saved State

`asw` stores pipeline progress in `.company/pipeline_state.json` and compares tracked inputs and outputs on later runs.

- Completed planning phases are skipped only when their tracked files still match.
- Phase-preparation steps and implementation turns are tracked too, not just the top-level planning phases.
- If tracked inputs changed but saved outputs still exist, `asw` prompts you to continue, rerun, or restart at the earliest affected step.
- `--restart` deletes `.company/` before the run begins.

For the full behavior, see [Runs, State, and Recovery](runs-and-state.md).

## See Also

- [Quickstart](../getting-started/quickstart.md) - a practical first run
- [Key Concepts](concepts.md) - the model behind the phases, loops, and gates
- [Runs, State, and Recovery](runs-and-state.md) - detailed resume, invalidation, and restart behavior
