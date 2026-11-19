---
name: designing-for-resilience
description: "Use when the diff adds a new hard dependency to a user-facing path, a fallback or default value on failure, a feature flag or kill switch, a circuit breaker, or something that was partitioned per zone, region, or tenant becoming global."
sources:
  - "SRE Book 2e Ch17"
  - "SRE Book Ch21, Ch22"
  - "google-sre-skills (MIT): designing-for-resilience"
  - "agent-skills-reliability (MIT): resilience-patterns"
---

## Applies when

- A request path now awaits a new service, store, or third-party call.
- Error handling adds a fallback, cached value, or default.
- A flag, kill switch, or emergency mode is introduced.
- A single queue, bucket, leader, or endpoint replaces per-domain ones.

## Questions to ask of the diff

- For each new dependency, read what the code does on timeout, on error, and on a slow but successful response. Are those three outcomes handled differently on purpose?
- Criticality: is a non-critical call (analytics, recommendations, audit fan-out, notifications) awaited inline on a critical path? It should be async or best-effort with a short timeout.
- Fallback safety: does the fallback get persisted? A default written back to storage turns a transient outage into permanent wrong data. Is stale data marked as stale?
- Kill switch: can the new feature be turned off without a deploy? What does the flag lookup return when the flag source is unreachable, and is that the safe side?
- Failure domains: does the change hardcode one region or zone, or route every tenant through one shared component?
- Control plane at request time: does serving now require a config, auth, or discovery service on every request instead of a cached copy with expiry?
- Circuit breaker: scoped per dependency, fails fast with a clear error, and probes recovery with limited traffic?

## Findings vs questions

- Finding: an inline await of a non-critical call on a critical handler (quote it), a fallback that writes its default to storage, a flag read whose default enables a risky path.
- Question: the intended behavior when a dependency is down, if the code does not make it explicit.

## Not a finding

- No circuit breaker when a timeout and bounded retry already exist on a non-critical dependency.
- Single-region design in a service that is clearly single-region by intent.
