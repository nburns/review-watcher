---
name: load-management
description: "Use when the diff changes load balancer or routing config, client connection reuse or pooling, service discovery or DNS caching, session affinity, failover targets, or the health endpoint a balancer uses."
sources:
  - "SRE Book Ch19, Ch20"
  - "Workbook Ch11"
  - "google-sre-skills (MIT): load-management"
  - "sre-practice-skills (Apache-2.0): sre-capacity-and-system-design"
---

## Applies when

- Balancer, ingress, proxy, or routing rules change.
- Client code adds long-lived or multiplexed connections, keep-alive, or a connection pool.
- Discovery, DNS resolution, or backend lists are cached or hardcoded.
- Failover or affinity (sticky sessions, hashing by user or tenant) is configured.

## Questions to ask of the diff

- Long-lived connections: does the client hold one connection per backend forever? After scale-out new instances get no traffic unless connections have a max age or the client rebalances.
- Balancing signal: round robin across uneven instances, or a least-loaded or least-outstanding policy? Round robin with slow instances piles requests on them.
- Health endpoint used by the balancer: if it depends on a shared dependency, that dependency failing marks every instance unhealthy at once and drains the whole pool. Is there a fail-open floor?
- Affinity and hashing: is the key skewed (one large tenant), creating a hot instance? What happens to its traffic when that instance leaves?
- Failover: when a zone or region drops, does the config send all its traffic to one target with no cap? Does the target have room?
- Discovery caching: does the client resolve once at startup and never again, so replaced backends are never seen?
- Retries: does a retry go to a different backend, or the same failing one?
- Deregistration: is the drain delay on scale-in at least the longest request timeout?

## Findings vs questions

- Finding: a balancer health path that calls a database or another service (quote the handler), a backend address resolved once at import time, a drain delay shorter than the request timeout set elsewhere (quote both).
- Question: traffic distribution, instance counts, and failover capacity that live outside the repo.

## Not a finding

- Single-instance or local-development configs.
- Default balancer policy when the repo shows no evidence of uneven backends.
