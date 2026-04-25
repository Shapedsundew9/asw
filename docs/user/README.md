# AgenticOrg CLI User Documentation

Learn how to install `asw`, run the current pipeline, review its gates, and work safely with the generated workspace.

`asw` orchestrates a simulated company of LLM-based agents. A run starts from a vision document, produces a PRD, architecture, and execution plan, expands the approved team into role prompts, bootstraps a validation contract, prepares per-phase delivery artifacts, and records implementation turns with validation and review evidence.

## Getting Started

| Document | Description |
|----------|-------------|
| [Installation](getting-started/installation.md) | Prerequisites, Gemini setup, installation, and CLI verification |
| [Quickstart](getting-started/quickstart.md) | First end-to-end run, founder gates, validation contract, and generated artifacts |

## Tutorials

| Document | Description |
|----------|-------------|
| [First Complete Run](tutorials/first-project.md) | A realistic walkthrough covering planning, phase preparation, implementation turns, and reruns |

## Reference

| Document | Description |
|----------|-------------|
| [CLI Reference](reference/cli.md) | Command syntax, flags, examples, and exit codes |
| [Key Concepts](reference/concepts.md) | Pipeline phases, founder gates, validation contracts, roles, and automatic commits |
| [Runs, State, and Recovery](reference/runs-and-state.md) | Resume behavior, invalidation prompts, deferred setup execution, and recovery patterns |

## What's Next

Start with [Installation](getting-started/installation.md), then run [Quickstart](getting-started/quickstart.md). If you are reviewing what changed on this branch, [Key Concepts](reference/concepts.md) and [Runs, State, and Recovery](reference/runs-and-state.md) explain the biggest user-visible differences.
