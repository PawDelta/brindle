# ADR 0002: Message queue for dispatch events

## Status

Accepted, 2024-05-03.

## Context

The dispatch service publishes route assignments that drivers' apps and the
billing service both consume. We compared a managed queue with running our own
broker.

## Decision

Use the managed queue (SQS) with a dead-letter queue after 5 failed receives.
Running our own broker was rejected because nobody wants to operate it.

## Consequences

Messages may be delivered more than once, so every consumer must be
idempotent. Ordering is per route, enforced with a message group id.
