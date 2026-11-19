---
name: reliable-launches
description: "Use when the diff introduces a new user-facing feature, endpoint, service, external integration, or shipped client behavior, or moves traffic to a new system, or the PR says launch, beta, or GA."
sources:
  - "SRE Book Ch27, Appendix E"
  - "SRE Book 2e Ch7, Ch20"
  - "google-sre-skills (MIT): reliable-launches"
  - "agent-skills-reliability (MIT): production-readiness-review"
---

## Applies when

- A new route, job, service, or integration appears.
- Client code (web, mobile, desktop, SDK) that cannot be recalled after release changes.
- Traffic or data moves from an old system to a new one.

## Questions to ask of the diff

- Exposure: is the feature behind a flag or allowlist that defaults off and can ramp? Or does merge equal launch to everyone?
- Expensive operations: can every user trigger a heavy operation (export, reindex, bulk send) at once on day one? Is there a per-user limit?
- New third-party call: what is its quota, and what does the code do when the quota is hit?
- Shipped clients: polling intervals, retry loops, and refresh timers in client code cannot be fixed by a server deploy. Are they jittered and backed off, and can the server slow them (retry hint, config)?
- Synchronized start: does a launch at a fixed time make every client fetch at once?
- Turn-off path: if the flag goes off after users created data with the feature, does the old path tolerate that data?
- Readiness in the same PR: are the metrics, log fields, and alert or SLO entries for the new path in this diff, or deferred? Grep for the new route or job name in alert and dashboard files.
- Migration of traffic: is there a dual-run or shadow phase and a way back?

## Findings vs questions

- Finding: a client poll or retry loop with a fixed interval and no backoff (quote it), a flag default that exposes the feature to all users, a new route absent from every alert file (state the search).
- Question: expected launch traffic, quota sizing, and the ramp plan.

## Not a finding

- Internal tools and admin pages with few users.
- Missing launch docs when the repo has no launch convention.
