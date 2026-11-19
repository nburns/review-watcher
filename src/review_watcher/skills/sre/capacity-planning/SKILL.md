---
name: capacity-planning
description: "Use when the diff changes per-request cost (extra queries or calls, queries in loops), work that grows with data size, replica or instance counts, resource requests and limits, autoscaling signals, or storage growth."
sources:
  - "SRE Book 2e Ch15"
  - "SRE Book Ch21, Ch22"
  - "Workbook Ch12"
  - "google-sre-skills (MIT): capacity-planning"
  - "agent-skills-reliability (MIT): capacity-planning"
  - "sre-practice-skills (Apache-2.0): sre-capacity-and-system-design"
---

## Applies when

- A handler gains queries, outbound calls, or loops over related records.
- Replica counts, CPU or memory requests and limits, or scaling policies change.
- New persistent data is written without a stated retention.

## Questions to ask of the diff

- Per-request multiplier: how many downstream calls or queries does one request now make? A query inside a loop over results scales with page size; a new call per request adds the full request rate to that dependency.
- Growth with data: does the work scan a table or list that grows with customers or time? It passes review today and fails at ten times the data.
- Memory per request: does it load a whole file, result set, or response body into memory?
- Failure-domain headroom: with the replica count and placement in the diff, can the remaining instances carry peak if one zone or instance is lost? A production minimum of one has no headroom.
- Scaling signal: CPU-based scaling for IO-bound or queue-driven work reacts late. Is the signal queue depth, concurrency, or latency where that fits?
- Scale lag: does the minimum cover a burst while new instances boot and warm up?
- Scale-down: is there a cooldown or hysteresis to avoid flapping?
- Limits lowered: does a reduced memory limit fit the peak measured or documented elsewhere in the repo?
- Cache dependence: if the cache is cold or evicted, what multiple of load hits the store behind it?

## Findings vs questions

- Finding: a query or call inside a loop over a result set (quote it), a production minimum replica count of one for a service the repo marks critical, an unpaginated scan of a growing table.
- Question: expected traffic and growth, and whether a load test backs the new limits.

## Not a finding

- Missing load tests for a small or internal change.
- Connection, quota, and burst items the main rubric already covers.
