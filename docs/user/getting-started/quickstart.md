# Quickstart

This guide takes you from zero to a completed V0.2 pipeline run in about five minutes.

## Before You Start

Complete [Installation](installation.md) first. Your Gemini CLI must be authenticated, and you should use an interactive terminal because the Founder Review Gate is menu-driven.

Confirm that the current shell can see your API key:

```bash
env | grep GEMINI_API_KEY
```

If that prints nothing, set it in this shell session first:

```bash
export GEMINI_API_KEY="your_api_key_here"
```

## Step 1 - Create A Project Folder

```bash
mkdir my-first-project
cd my-first-project
git init
git commit --allow-empty -m "Initial commit"
```

If you only want to experiment, you can skip the git setup above and add `--no-commit` in Step 3.

## Step 2 - Write A Vision Document

Create a file named `vision.md`. A vision document is a short plain-English brief for the founding team that `asw` simulates.

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

There is no rigid template. Write whatever clearly explains what you want to build and who it is for.

## Step 3 - Start The Pipeline

```bash
asw start --vision vision.md
```

Optional: capture a debug log while you learn the workflow.

```bash
asw start --vision vision.md --debug
```

The CLI prints progress like this:

```text
========================================================================
  AgenticOrg CLI – V0.2 Pipeline
========================================================================
✓ Vision loaded: vision.md (604 chars)
✓ LLM backend: Gemini CLI

✓ Company directory initialised: /path/to/my-first-project/.company

>> CPO – attempt 1
   Invoking CPO via Gemini CLI (may take up to 5 min)…
   Response received.
   Lint passed for PRD.

✓ PRD written: /path/to/my-first-project/.company/artifacts/prd.md
```

## Step 4 - Answer Questions And Review The PRD

The CPO may include structured founder questions in the PRD. When that happens, `asw` asks those questions first, writes your answers back into the artifact locally, and then returns you to the review flow.

Once the artifact is ready for review, you can choose from these actions:

| Choice | What Happens |
|--------|--------------|
| Approve | Accept the artifact and continue |
| Reject | Rerun the phase from the original context |
| Modify | Provide notes and rerun the phase with your guidance |
| Request More Questions | Ask the agent for another question round focused on unresolved issues |
| Stop | Exit cleanly with everything created so far left on disk |

When you use **Modify**, type your feedback in the multiline prompt and press `Esc`, then `Enter`, to submit it.

## Step 5 - Review The Architecture

After the PRD is approved, the CTO generates:

- `.company/artifacts/architecture.json`
- `.company/artifacts/architecture.md`

Review the technical choices, component breakdown, data models, API contracts, and Mermaid diagram before approving.

## Step 6 - Review The Execution Plan

After the architecture is approved, the VP Engineering produces:

- `.company/artifacts/execution_plan.json`
- `.company/artifacts/execution_plan.md`

This is the phase where you approve the first-phase team and the overall build-up strategy.

Useful review questions:

- Does Phase 1 stay intentionally narrow enough to validate the product quickly?
- Are any roles being hired too early?
- Are the deferred capabilities clearly justified?

If you want direct control, you can paste an edited execution-plan JSON object during **Modify** and `asw` validates it locally.

## Step 7 - Let Hiring And Role Generation Finish

Once you approve the execution plan, the Hiring Manager automatically expands the approved team into detailed role briefs, and the Role Writer then generates one Markdown role prompt for each approved entry. These phases run automatically with no extra Founder Review Gate.

## What Gets Created

After the pipeline completes, your project looks like this:

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
      python_backend_developer.md
      frontend_developer.md
    artifacts/
      prd.md
      architecture.json
      architecture.md
      execution_plan.json
      execution_plan.md
      roster.json
      roster.md
    memory/
    templates/
    standards/
```

If you did not use `--no-commit`, your git history will usually contain four automatic commits:

```text
[asw] Phase: prd-generation completed
[asw] Phase: architecture-generation completed
[asw] Phase: execution-plan-generation completed
[asw] Phase: hiring completed
```

By default those commits stage only `.company/`. If you explicitly want the phase commits to include the rest of the repository worktree, rerun with `--stage-all`.

## If You Run It Again

Rerunning the same command resumes from saved state. If completed artifacts are still present, `asw` skips those phases, including generated roles when the expected role files from the approved roster still exist. If the vision file changed, `asw` asks whether to continue or restart from scratch.

The same skip rule still applies if you edited `.company/templates/` or
`.company/standards/` after the last run. Use `--restart` when you want
template or standards changes to regenerate execution plans, role briefs, or
role prompts without manually removing artifacts.

Use `--restart` when you want to discard the existing `.company/` directory and rebuild it from scratch.

## Troubleshooting

If you see an error about `GEMINI_API_KEY`:

1. Set the key in the same shell where you run `asw`.
2. Re-check with `env | grep GEMINI_API_KEY`.
3. Run `gemini -p "Reply with OK" -o json` to verify Gemini works directly.
4. Rerun `asw start --vision vision.md --no-commit`.

If you want more detail about what happened during a failed run, rerun with `--debug`.

If a generated artifact fails structural validation, `asw` saves the rejected output under `.company/artifacts/failed/` before exiting so you can inspect what went wrong.

## What's Next

- [First Complete Run](../tutorials/first-project.md) - a deeper walkthrough with commentary on each decision
- [Runs, State, and Recovery](../reference/runs-and-state.md) - resume, restart, and debug behavior
- [CLI Reference](../reference/cli.md) - full flag documentation
