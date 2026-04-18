# ASW-CORE-002 Change Log

## Pipeline Resume & Restart

### Added

- **`pipeline_state.json`** — checkpoint file written to `.company/` after each phase. Tracks pipeline version, vision file SHA-256 hash, and completed phases with timestamps.
- **Resume-by-default** — `run_pipeline()` reads existing state and skips phases that are already completed (both recorded in state AND artifacts exist on disk). Existing artifacts are loaded from disk to provide context for downstream phases.
- **Vision change detection** — SHA-256 hash of the vision file is stored in state. On re-invocation, if the hash differs, the user is prompted to **[C]ontinue** from where they left off or **[R]estart** from scratch.
- **`--restart` CLI flag** — deletes the entire `.company/` directory before the pipeline begins, forcing a clean slate.
- **State functions in `company.py`**:
  - `hash_file(path)` — SHA-256 hex digest of a file.
  - `read_pipeline_state(workdir)` — read and parse state file; returns `None` if missing or corrupt.
  - `write_pipeline_state(workdir, state)` — persist state dict to disk.
  - `mark_phase_complete(workdir, state, phase)` — record a phase as completed with timestamp.
  - `clear_company(workdir)` — delete the `.company/` directory.
- **`_is_phase_done()` in `orchestrator.py`** — checks both state and artifact file existence; a missing artifact triggers re-run even if state says completed.

### Changed

- `run_pipeline()` — accepts new `restart` keyword argument; orchestration flow now wraps each phase in a skip-or-run check.
- `build_parser()` in `cli/main.py` — added `--restart` argument to the `start` subcommand.

### Tests

- `test_company.py` — 8 new tests for `hash_file`, `read_pipeline_state`, `write_pipeline_state`, `mark_phase_complete`, `clear_company`.
- `test_orchestrator.py` — 8 new tests: `_is_phase_done` unit tests, resume skipping, missing-artifact re-run, `--restart` flag, vision-changed continue/restart flows.
