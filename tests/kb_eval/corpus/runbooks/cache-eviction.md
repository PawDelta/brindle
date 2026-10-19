# Cache eviction storms

## Symptoms

The Redis hit rate drops below 60 percent and the route-quote API latency p95
climbs past 800 ms. Memory on the cache nodes sits at the `maxmemory` limit.

## Mitigation

Raise `maxmemory` on the cache cluster by one node size using
`nwctl cache scale --up`. Do not flush the cache: a cold cache sends the full
load to Postgres.

## Root causes seen before

A batch job that wrote one key per parcel with no expiry filled the cache in
March. Keys for parcel tracking must set a TTL of 6 hours.
