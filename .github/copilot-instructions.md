# Repository Context for AI Agents

## Project status: new and under active development

This repository is **new and under active development**. There are no external
consumers, no published releases, and no stability guarantees yet.

## No backwards compatibility requirement

**API contracts and data structure contracts do not need to be preserved.**
Backwards compatibility is **not** a goal of this project at this stage.

## Design freedom and mandate

The overriding objective is **clean, simple, robust design**. Agents working in
this repository have **explicit freedom — and are expected — to design and
redesign** whatever is necessary to meet that objective, including:

- Changing or replacing data structures, schemas, and on-disk/serialized formats.
- Refactoring, renaming, or removing public APIs, modules, traits, and types.
- Restructuring crates, files, and module hierarchies.
- Deleting code, tests, or documentation that no longer fits a cleaner design.

If a cleaner, simpler, or more robust solution requires breaking changes, **make
the breaking change**. Do not add compatibility shims, deprecation layers, or
migration code unless explicitly requested. Do not preserve an awkward API
"just in case" something depends on it — nothing external does.

## What this does NOT mean

- It does **not** mean skip tests, skip review of correctness, or bypass safety
  checks (e.g. `--no-verify`, force pushes, destroying user data).
- It does **not** mean make sweeping unrelated changes while doing a small task.
  Stay scoped to the user's request, but within that scope choose the cleanest
  design rather than the most backwards-compatible one.
- It does **not** override the operational safety rules around destructive
  actions on shared systems (force pushes, dropping shared data, etc.) — those
  still warrant confirmation.

## Summary

Prefer the clean redesign over the compatible patch. Assume no external callers.
Optimize for the long-term shape of the codebase, not for preserving today's
shape.
