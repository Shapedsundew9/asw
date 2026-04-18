# CLI Reference

Complete reference for all `asw` commands and flags.

## Global Synopsis

```bash
asw [-h] <command> [options]
```

| Flag | Description |
|------|-------------|
| `-h`, `--help` | Print help and exit |

## Commands

### `asw start`

Start the agentic SDLC pipeline from a vision document.

```bash
asw start --vision <path> [--workdir <path>]
```

#### Flags

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--vision VISION` | Yes | — | Path to the vision Markdown file. Can be relative or absolute. |
| `--workdir WORKDIR` | No | Current directory | Working directory where `.company/` state is created. |

#### Examples

Run from the current directory using a vision file in the same folder:

```bash
asw start --vision vision.md
```

Point to a vision file and an explicit working directory:

```bash
asw start --vision ~/ideas/saas-tool.md --workdir ~/projects/saas-tool
```

#### Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Pipeline completed successfully |
| `1` | A startup check failed (e.g. vision file not found, workdir missing) |

The pipeline also calls `sys.exit(0)` when the Founder chooses **[S]top** at a review gate, and `sys.exit(1)` when an agent fails to produce valid output after all retries are exhausted.

## Environment Requirements

- The `gemini` CLI must be installed and on `$PATH` before running any command.
- `--workdir` (or the current directory) must be inside a git repository.

## See Also

- [Key Concepts](concepts.md) — the pipeline, agents, review gates, and `.company/` directory
- [Quickstart](../getting-started/quickstart.md) — a practical first-run walkthrough
