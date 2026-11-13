---
name: reviewer-light
description: Reviews small or docs/test-only branches with Sonnet at low effort
extends: reviewer
provider: claude
model: sonnet
effort: low
---
Use the scoped context pack in your task as the starting point. Read beyond
the touched files and listed callers only to investigate a concrete risk.
