# Installation

This guide covers the prerequisites, installation steps, and verification commands needed before you run `asw`.

## Prerequisites

| Requirement | Minimum Version | Purpose |
|-------------|-----------------|---------|
| Python | 3.14+ | Runtime for `asw` itself |
| Node.js | 18+ | Required by the Gemini CLI |
| Google Gemini CLI | latest | LLM backend |
| Git | any recent | Phase commits when you are not using `--no-commit` |
| Interactive terminal | any recent shell | Founder Review Gate menus and prompts |

## Install The Gemini CLI

`asw` uses the [Google Gemini CLI](https://github.com/google-gemini/gemini-cli) as its only LLM backend.

```bash
npm install -g @google/gemini-cli
```

Verify that it is on your `PATH`:

```bash
gemini --version
```

If the command is not found, ensure your npm global bin directory is on `PATH`.

## Authenticate Gemini For Headless Runs

`asw` calls Gemini in headless mode with `gemini -p ... -o json`. In that mode, Gemini must see `GEMINI_API_KEY` in the same shell session.

Set it for the current shell:

```bash
export GEMINI_API_KEY="your_api_key_here"
```

Verify that the variable is visible before running `asw`:

```bash
env | grep GEMINI_API_KEY
```

Quick end-to-end check:

```bash
gemini -p "Reply with OK" -o json
```

If that command fails with exit code `41`, Gemini still cannot see your API key in the current shell.

## Install `asw`

Clone the repository and install it in editable mode inside a virtual environment:

```bash
git clone https://github.com/Shapedsundew9/asw.git
cd asw
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

On Windows PowerShell, activate the environment with:

```powershell
.venv\Scripts\Activate.ps1
```

## Verify The CLI

Run the top-level help:

```bash
asw --help
```

Current output:

```text
usage: asw [-h] <command> ...

AgenticOrg CLI – orchestrate a simulated company of LLM-based software
development agents.

positional arguments:
  <command>
    start     Start the agentic SDLC pipeline from a vision document.

options:
  -h, --help  show this help message and exit

Use 'asw <command> --help' for command-specific options. Example: 'asw start
--help'. Tip: use 'asw start --no-commit' to run without requiring a git
repository.
```

Inspect the `start` command flags:

```bash
asw start --help
```

You should see `--vision`, `--workdir`, `--no-commit`, `--restart`, and `--debug [LOGFILE]`.

## Git Requirement For Working Directories

By default, `asw` expects the working directory to be inside a git repository because it commits phase results automatically.

Initialize git if you want those commits:

```bash
git init
git commit --allow-empty -m "Initial commit"
```

If you only want to experiment, you can skip git entirely and run with `--no-commit`.

## What's Next

- [Quickstart](quickstart.md) - run your first complete pipeline
- [Runs, State, and Recovery](../reference/runs-and-state.md) - understand reruns, restarts, and debug logs
- [Key Concepts](../reference/concepts.md) - learn how phases, gates, and artifacts fit together
