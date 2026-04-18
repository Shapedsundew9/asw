# Key Concepts

A quick reference for the ideas that underpin how `asw` works.

## The Pipeline

When you run `asw start`, it executes a fixed sequence of phases called the **V0.1 Pipeline**. Each phase is owned by an agent, produces an artifact, and must pass a Founder Review Gate before the pipeline advances.

```mermaid
%%{init: {'theme': 'dark', 'themeVariables': {'primaryColor': '#2d6a4f', 'primaryTextColor': '#d8f3dc', 'primaryBorderColor': '#52b788', 'lineColor': '#74c69d', 'secondaryColor': '#1b4332', 'tertiaryColor': '#40916c', 'edgeLabelBackground': '#1b4332'}}}%%
graph TD
    A([Vision Document]) --> B[CPO Agent]
    B --> C{Mechanical Lint}
    C -- Fail up to 2 retries --> B
    C -- Pass --> D[Founder Review Gate]
    D -- Approve --> E[Git Commit]
    D -- Reject --> B
    D -- Modify --> B
    D -- Stop --> Z([Exit])
    E --> F[CTO Agent]
    F --> G{Mechanical Lint}
    G -- Fail up to 2 retries --> F
    G -- Pass --> H[Founder Review Gate]
    H -- Approve --> I[Git Commit]
    H -- Reject --> F
    H -- Modify --> F
    H -- Stop --> Z
    I --> J([Pipeline Complete])
```

### Phase A — PRD

The **CPO agent** reads your vision document and produces a **Product Requirements Document** in Markdown. The PRD must contain all of the following sections:

- Executive Summary
- Goals & Success Metrics
- Target Users
- Functional Requirements
- Non-Functional Requirements
- User Stories
- Acceptance Criteria Checklist
- System Overview Diagram
- Risks & Mitigations
- Open Questions

### Phase B — Architecture

The **CTO agent** reads the vision and the approved PRD, then produces a **system architecture** containing:

- An `architecture.json` file describing the tech stack, components, data models, API contracts, and deployment strategy.
- An `architecture.md` file with a Mermaid component diagram.

---

## Agents and Roles

Each agent is a specialised LLM session guided by a **role file** — a Markdown document that defines the agent's persona, output format, and strict rules.

Role files live in `.company/roles/` and are copied there from the package defaults when you first run `asw start`. You can edit them between runs to change agent behaviour.

| Agent | Role File | Artifact Produced |
|-------|-----------|-------------------|
| CPO | `.company/roles/cpo.md` | `.company/artifacts/prd.md` |
| CTO | `.company/roles/cto.md` | `.company/artifacts/architecture.json` + `architecture.md` |

---

## Mechanical Linting

Before an agent's output reaches the Founder Review Gate, `asw` runs **mechanical linters** to verify structural correctness. If linting fails the agent is sent feedback and retried automatically (up to two retries). If it still fails after all retries the pipeline exits with an error.

Linting checks include:

- All required Markdown sections are present (PRD phases).
- The Acceptance Criteria Checklist uses `- [x]` items.
- A valid fenced Mermaid code block is present.
- The architecture JSON block is present and parses correctly.

---

## Founder Review Gate

At every major phase boundary the pipeline **pauses** and presents the artifact for your review. This is called the **Founder Review Gate**.

```text
========================================================================
  FOUNDER REVIEW GATE  –  Phase: PRD
========================================================================
[A]pprove  [R]eject  [M]odify  [S]top  >
```

| Choice | Behaviour |
|--------|-----------|
| **A** — Approve | Accept the artifact, commit to git, advance the pipeline |
| **R** — Reject | Discard the artifact; agent starts over with the original context |
| **M** — Modify | You type multi-line feedback; agent retries with your notes included |
| **S** — Stop | Pipeline exits cleanly with code `0`; all prior commits are preserved |

When you choose **M**, type your feedback line by line and press **Enter on a blank line** to finish.

---

## The `.company/` Directory

`asw` keeps all shared state in a `.company/` directory inside your working directory. It is created automatically on first run.

```text
.company/
  roles/        ← Agent system prompts (editable)
  artifacts/    ← Documents produced by agents
  state/        ← Internal pipeline state (managed by asw)
```

This directory is committed to git at the end of each successful phase, giving you a full history of every artifact.

---

## The Git State Machine

`asw` automatically stages and commits `.company/` (and `src/` if it exists) after each approved phase. Commit messages follow this pattern:

```text
[asw] Phase: prd-generation completed
[asw] Phase: architecture-generation completed
```

Your working directory must be inside a git repository before you start. If there is nothing new to commit (e.g. the agent produced identical output on a retry), `asw` skips the commit silently.

### Skipping Commits

Pass `--no-commit` to disable all git operations for a run:

```bash
asw start --vision vision.md --no-commit
```

This is useful when you want to explore agent output before deciding whether to track it, or when running `asw` outside a git repository for quick experiments. The git-repo check is also skipped when `--no-commit` is set.

---

## LLM Backend

`asw` currently supports a single backend: the **Google Gemini CLI** (`gemini`). It must be installed and on `$PATH`.

```bash
npm install -g @google/gemini-cli
```

The backend is selected internally — there is no user-facing flag to change it in V0.1.

---

## See Also

- [CLI Reference](cli.md) — all commands and flags
- [Quickstart](../getting-started/quickstart.md) — a first-run walkthrough
- [First Project Tutorial](../tutorials/first-project.md) — end-to-end guided example
