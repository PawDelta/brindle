# ADR 0001: Use Postgres for order data

## Status

Accepted, 2024-02-12.

## Context

Order and shipment data is relational and needs transactions across parcels,
invoices and routes. The team already runs Postgres for billing.

## Decision

Store orders in Postgres 16 with one logical cluster named `orders`. Reject
document stores for this data.

## Consequences

Schema migrations need review from the data platform team. Read replicas serve
reporting queries so they never touch the primary.
