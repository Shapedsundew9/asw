# Installation

This document covers every prerequisite and install step needed before you can run `asw`.

## Prerequisites

| Requirement | Minimum Version | Purpose |
|-------------|-----------------|---------|
| Python | 3.14+ | Runtime for `asw` itself |
| Node.js | 18+ | Required by the Gemini CLI |
| Google Gemini CLI | latest | LLM backend |
| Git | any recent | Auto-committing phase results |

### Install the Gemini CLI

`asw` uses the [Google Gemini CLI](https://github.com/google-gemini/gemini-cli) as its LLM backend.

```bash
npm install -g @google/gemini-cli
```

Verify it is available on your PATH:

```bash
gemini --version
```

If the command is not found, ensure your npm global bin directory is on `$PATH`.

### Install `asw`

Clone the repository and install it in editable mode inside a virtual environment:

```bash
git clone https://github.com/Shapedsundew9/asw.git
cd asw
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
```

Verify the install:

```bash
asw --help
```

You should see:

```text
usage: asw [-h] {start} ...

AgenticOrg CLI – orchestrate a simulated company of LLM-based software
development agents.

positional arguments:
  {start}
    start     Start the agentic SDLC pipeline from a vision document.

options:
  -h, --help  show this help message and exit

Tip: use 'asw start --no-commit' to run without requiring a git repository.
```

## Working-Directory Requirement

Every project you run `asw` against must be inside a **git repository**. `asw` commits results to git at the end of each pipeline phase. If your project folder is not yet a repo, initialise one before running:

```bash
git init
git commit --allow-empty -m "Initial commit"
```

## What's Next

- [Quickstart](quickstart.md) — run your first pipeline in under five minutes
- [Key Concepts](../reference/concepts.md) — understand what `asw` actually does before you dive in
