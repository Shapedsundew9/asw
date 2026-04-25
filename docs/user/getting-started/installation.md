# Installation

Install the prerequisites for `asw`, authenticate Gemini, and verify the CLI before your first run.

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Python 3.14+ | Required by the package metadata in `pyproject.toml` |
| Node.js | Required by the Google Gemini CLI |
| Google Gemini CLI | The only LLM backend exposed by the current CLI |
| Git | Required unless you plan to run with `--no-commit` |
| Interactive terminal | Required for Founder review menus and prompts |

## Install The Gemini CLI

`asw` currently uses the [Google Gemini CLI](https://github.com/google-gemini/gemini-cli) as its only LLM backend.

```bash
npm install -g @google/gemini-cli
```

Verify that it is available on your `PATH`:

```bash
gemini --version
```

If the command is not found, add your npm global bin directory to `PATH` and try again.

## Authenticate Gemini For Headless Runs

`asw` calls Gemini in headless mode with `gemini -p ... -o json`. Gemini must see `GEMINI_API_KEY` in the same shell session where you run `asw`.

Set it for the current shell:

```bash
export GEMINI_API_KEY="your_api_key_here"
```

Confirm that the variable is visible:

```bash
env | grep GEMINI_API_KEY
```

Run a direct Gemini check before you troubleshoot `asw` itself:

```bash
gemini -p "Reply with OK" -o json
```

If that command fails, fix Gemini authentication first.

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

Check the top-level help:

```bash
asw --help
```

Then inspect the only current subcommand:

```bash
asw start --help
```

The `start` help should list these flags:

- `--vision`
- `--workdir`
- `--no-commit`
- `--stage-all`
- `--restart`
- `--execute-phase-setups`
- `--debug [LOGFILE]`

`--execute-phase-setups` is an advanced opt-in flag. Without it, `asw` still generates per-phase setup proposals and scripts, but records setup execution as deferred instead of running those scripts.

## Understand The Git Requirement

By default, `asw` expects the working directory to be inside a git repository because it creates automatic phase and implementation-turn commits.

Initialize git if you want those commits:

```bash
git init
git commit --allow-empty -m "Initial commit"
```

If you only want to experiment, skip git entirely and run with `--no-commit`.

If you want automatic commits to include changes outside `.company/`, add `--stage-all` when you run the pipeline.

## What's Next

- [Quickstart](quickstart.md) - run your first end-to-end pipeline
- [CLI Reference](../reference/cli.md) - see the full command surface
- [Key Concepts](../reference/concepts.md) - understand the phases, gates, and artifacts before your first real run
