# Generic Python Developer Container Agents

This repository uses a Debian-based VS Code Dev Container built on Microsoft's latest stable Python image.

## Agents

### 1) Python Developer Agent
- **Scope:** Implement Python features, refactors, and bug fixes inside the container.
- **Skill criteria:** Python 3, packaging basics, virtual environment hygiene, debugging, and clean code practices.

### 2) Python Quality Agent
- **Scope:** Run and interpret linting, formatting, and static analysis workflows configured for the project.
- **Skill criteria:** Familiarity with common Python quality tools (for example: `ruff`, `pytest`, type checking tools) and CI feedback loops.

### 3) Python Test Agent
- **Scope:** Add and update targeted tests for changed behavior and verify regressions are not introduced.
- **Skill criteria:** Unit/integration testing patterns, fixture design, and reproducible test execution in containerized environments.

## Operational Boundaries
- Work only within repository files and configured development tooling.
- Prefer minimal, reversible changes with explicit validation steps.
- Use installed CLI tooling (`rg`, Gemini CLI, GitHub Copilot CLI) for agentic workflows and code discovery.
