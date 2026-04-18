# Quickstart

This guide takes you from zero to a completed pipeline run in about five minutes.

## Before You Start

Complete [Installation](installation.md) first. Your Gemini CLI must be authenticated and your project folder must be a git repository.

Before running the pipeline, confirm the current shell can see your API key:

```bash
env | grep GEMINI_API_KEY
```

If this prints nothing, set it in this shell session first:

```bash
export GEMINI_API_KEY="your_api_key_here"
```

## Step 1 — Create a Project Folder

```bash
mkdir my-first-project
cd my-first-project
git init
git commit --allow-empty -m "Initial commit"
```

> **Tip:** If you just want to experiment without creating a git repo, add `--no-commit` to the `asw start` command in Step 3. The git setup above can then be skipped.

## Step 2 — Write a Vision Document

Create a file called `vision.md`. A vision document is a short plain-English description of what you want to build — think of it as a brief you hand to a founding team.

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

There is no rigid template — write whatever clearly describes your intent. The CPO agent will fill in the details.

## Step 3 — Start the Pipeline

```bash
asw start --vision vision.md
```

`asw` will print its progress as it works:

```text
========================================================================
  AgenticOrg CLI – V0.2 Pipeline
========================================================================

✓ Company directory initialised: /path/to/my-first-project/.company
✓ Vision loaded: vision.md (604 chars)
✓ LLM backend: Gemini CLI

>> CPO – attempt 1
   Invoking CPO via Gemini CLI (may take up to 5 min)…
   Response received.
   Lint passed for PRD.

✓ PRD written: .company/artifacts/prd.md
```

## Step 4 — Review at Each Gate

After each phase, the pipeline pauses and asks for your decision:

```text
========================================================================
  FOUNDER REVIEW GATE  –  Phase: PRD
========================================================================

Artifact: .company/artifacts/prd.md

# Product Requirements Document
...

------------------------------------------------------------------------
[A]pprove  [R]eject  [M]odify  [S]top  >
```

| Key | Action |
|-----|--------|
| `A` | Approve — pipeline continues to the next phase |
| `R` | Reject — agent regenerates the artifact from scratch |
| `M` | Modify — you type feedback, agent retries incorporating your notes |
| `S` | Stop — pipeline exits immediately, nothing is lost |

After approving, `asw` commits the artifact to git and moves on.

## Step 5 — Review the Architecture

After the PRD is approved, the CTO agent runs and produces a system architecture. A second Founder Review Gate pauses the pipeline for your approval.

## Step 6 — Review the Roster

After the architecture is approved, the Hiring Manager agent proposes a roster of specialist roles needed to implement the system. A third Founder Review Gate pauses for your approval — this is a budget/headcount decision. You can use **Modify** to directly add, remove, or rename roles before approving.

## Step 7 — Automatic Role Generation

Once you approve the roster, the Role Writer agent generates a Markdown system prompt for each role — one LLM call per role. This runs automatically with no further review gates.

## What Gets Created

After both phases complete your project looks like this:

```text
my-first-project/
  vision.md
  .company/
    roles/
      cpo.md                        ← CPO system prompt
      cto.md                        ← CTO system prompt
      hiring_manager.md             ← Hiring Manager system prompt
      role_writer.md                ← Role Writer system prompt
      python_backend_developer.md   ← Generated role (example)
      frontend_developer.md         ← Generated role (example)
    artifacts/
      prd.md                        ← Product Requirements Document
      architecture.json             ← Architecture spec
      architecture.md               ← Architecture diagram (Mermaid)
      roster.json                   ← Approved roster
      roster.md                     ← Roster summary
    memory/                         ← Living documents
    templates/                      ← Reusable Markdown structures
    standards/                      ← Organisational guidelines
```

Your git log will contain three auto-commits:

```text
[asw] Phase: prd-generation completed
[asw] Phase: architecture-generation completed
[asw] Phase: hiring completed
```

## What's Next

- [First Project Tutorial](../tutorials/first-project.md) — a deeper walkthrough with commentary on each decision
- [CLI Reference](../reference/cli.md) — full flag documentation
- [Key Concepts](../reference/concepts.md) — the pipeline, roles, and review gates explained

## Troubleshooting

If you see an error like `Gemini CLI exited with code 41` and a message about `GEMINI_API_KEY`:

1. Set the key in the same shell where you run `asw`.
2. Re-check with `env | grep GEMINI_API_KEY`.
3. Run `gemini -p "Reply with OK" -o json` to verify Gemini works directly.
4. Re-run `asw start --vision vision.md --no-commit`.
