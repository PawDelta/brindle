# ADR 0003: Feature flags

## Status

Accepted, 2024-09-18.

## Context

Releases of the driver app were blocked on the slowest feature. We wanted to
ship code dark and turn it on per customer.

## Decision

Adopt a self-hosted flag service. Flags are boolean, owned by a team, and
reviewed every quarter. A flag older than 90 days after full rollout must be
removed from the code.

## Consequences

Tests must cover both flag states for changes behind a flag. The flag service
becomes a dependency of the dispatch service, so it falls back to the last
known values if unreachable.
