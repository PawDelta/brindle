---
name: developer-codex
description: Implements a well-scoped coding task on its own branch, using Codex (the medium tier of weight routing)
extends: developer
provider: codex
---
Run tests with `python -m pytest ...`, not `uv run pytest ...`: under Codex's
sandbox `uv` crashes reading the macOS system configuration, and the project's
venv is already first on your PATH.
