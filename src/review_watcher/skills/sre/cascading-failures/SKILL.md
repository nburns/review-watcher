---
name: cascading-failures
description: "Use when the diff adds or changes retry wrappers, timeouts or deadlines, parallel fan-out to backends, readiness checks, cache flushes or warmup, or fixed-interval timers that many instances run at once."
sources:
  - "SRE Book Ch22"
  - "SRE Book 2e Ch17"
  - "google-sre-skills (MIT): cascading-failures"
  - "agent-skills-reliability (MIT): resilience-patterns, capacity-planning"
  - "sre-practice-skills (Apache-2.0): sre-capacity-and-system-design"
---

## Applies when

- A retry loop, retrying client, or retry decorator is added or its attempt count changes.
- A timeout, deadline, or cancellation value changes on either side of a call.
- One request starts calling several backends, or a loop calls a backend per item.
- A readiness or liveness check, cache clear, or startup path changes.

## Questions to ask of the diff

- Retry layering: follow the call chain from entrypoint to the changed call and grep each layer for retry, attempts, backoff. Multiply attempts per layer; 3 layers of 3 is 27 calls per user request. Is only one layer retrying?
- Do retried statuses include overload signals (429, 503, "unavailable") without honoring a retry-after hint? Lower layers should pass "overloaded, do not retry" up instead.
- Timeout ordering: inner timeout times attempts, plus backoff, must fit inside the caller's timeout. Otherwise the callee keeps working after the caller gave up.
- Is the remaining deadline passed down, and does the server skip work whose deadline already passed?
- Does a readiness check now fail when the process is merely busy (queue depth, latency, pool exhausted)? Pulling busy instances shifts their load onto survivors.
- Synchronization: fixed TTLs on hot keys, timers on round minutes, reconnect loops with no jitter.
- Recovery: does a deploy or restart start with a cold cache in front of the database, or flush a whole cache at once?
- Fan-out: does one failed or slow backend fail the whole request, or are partial results allowed?

## Findings vs questions

- Finding: a retry wrapper around a call whose client already retries (quote both lines), an inner timeout larger than the outer one (quote both values), a reconnect or retry loop with a constant sleep.
- Question: amplification that depends on retries outside the repo (proxy, mesh, SDK defaults), or on traffic levels the diff does not show.

## Not a finding

- A single retry layer with bounded attempts and jittered backoff.
- Retries in CLIs, migrations, or one-off scripts with no concurrent callers.
- Identical TTLs on a few rarely read keys.
