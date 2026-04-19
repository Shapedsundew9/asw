# CLI Reference

This page documents the current `asw` command surface, supported flags, examples, and exit behavior.

## Global Synopsis

```bash
asw [-h] <command> ...
```

Use `asw <command> --help` to inspect command-specific flags.

## Commands

### `asw start`

Start the agentic SDLC pipeline from a vision document.

```bash
asw start [-h] --vision VISION [--workdir WORKDIR] [--no-commit] [--stage-all] [--restart] [--debug [LOGFILE]]
```

#### Flags

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--vision VISION` | Yes | none | Path to the vision Markdown file. Relative and absolute paths both work. |
| `--workdir WORKDIR` | No | current directory | Working directory where `.company/` is created and git operations run. |
| `--no-commit` | No | off | Skip git commits at phase boundaries. Also skips the git-repository requirement. |
| `--stage-all` | No | off | Stage the full git worktree during phase commits. Without this flag, `asw` stages only `.company/`. |
| `--restart` | No | off | Delete the existing `.company/` directory before starting the run. |
| `--debug [LOGFILE]` | No | off | Enable debug logging. If you omit `LOGFILE`, `asw` creates a timestamped log file in the current directory. If you pass a custom path, `asw` creates missing parent directories automatically. |

#### Examples

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

Stage the full repository during phase commits:

```bash
asw start --vision vision.md --stage-all
```

Write debug logs to an automatically named file:

```bash
asw start --vision vision.md --debug
```

Write debug logs to a specific file:

```bash
asw start --vision vision.md --debug asw.log
```

Discard the existing `.company/` state and rebuild from scratch:

```bash
asw start --vision vision.md --restart
```

## Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Pipeline completed successfully, or the Founder stopped the pipeline at a review gate |
| `1` | Startup validation failed, git commit failed, an artifact failed mechanical linting, or Gemini failed in a non-retryable or exhausted-retry state |

## Re-Runs And Saved State

`asw` stores pipeline progress in `.company/pipeline_state.json`.

On a later run:

- `pipeline_state.json` records a tracked-file hash catalog plus per-phase input and output hash snapshots.
- PRD, architecture, execution plan, roster, and role generation are skipped only when their saved snapshots still match the current tracked files.
- If tracked inputs for a completed phase changed, `asw` stops at the earliest affected phase and lets you continue with saved artifacts, rerun that phase, or restart from scratch.
- `--restart` bypasses saved state by deleting `.company/` before the run begins.

When structural linting fails, `asw` also saves the rejected output under `.company/artifacts/failed/` before exiting.

For a full explanation, see [Runs, State, and Recovery](runs-and-state.md).

## Environment Requirements

- `gemini` must be installed and available on `PATH`.
- `GEMINI_API_KEY` must be exported in the same shell session that runs `asw`.
- Use an interactive terminal because Founder Review Gate actions and question prompts are menu-driven.
- The working directory must be inside a git repository unless you pass `--no-commit`.

Quick verification:

```bash
env | grep GEMINI_API_KEY
gemini -p "Reply with OK" -o json
```

## See Also

- [Quickstart](../getting-started/quickstart.md) - a practical first run
- [Key Concepts](concepts.md) - phases, review gates, and generated artifacts
- [Runs, State, and Recovery](runs-and-state.md) - resume, restart, and debug behavior
