# Database failover

## When to fail over

Fail over the primary Postgres cluster when replication lag stays above 30
seconds for five minutes, or when the primary stops answering health checks
from two availability zones.

## Steps

1. Page the database on-call in the `#db-oncall` channel.
2. Run `nwctl db promote --cluster orders` to promote the standby in zone b.
3. Point the connection pooler at the new primary with `nwctl pooler repoint`.
4. Confirm writes succeed by creating a test shipment in staging tooling.

## After the failover

Rebuild the old primary as a standby within 24 hours. File an incident review
if the failover took longer than 10 minutes end to end.
