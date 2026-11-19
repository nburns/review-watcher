---
name: monitoring-observability
description: "Use when the diff adds or changes metrics, histograms, log statements, trace spans or context propagation, or adds a new request path, pool, queue, or async boundary that will need instrumenting."
sources:
  - "SRE Book Ch6"
  - "Workbook Ch4"
  - "SRE Book 2e Ch9"
  - "google-sre-skills (MIT): monitoring-observability"
  - "agent-skills-reliability (MIT): observability-instrumentation"
---

## Applies when

- Metric, span, or log calls are added, renamed, or removed.
- A new handler, outbound call, pool, queue, or scheduled job appears.
- Work moves across an async boundary (queue, thread, callback).

## Questions to ask of the diff

- Signal coverage for the new path: request count, error count, latency distribution, and a saturation gauge for any new pool or queue. Grep for an existing helper the siblings use.
- Latency split: is duration recorded on the failure path too, and labeled by outcome? Fast errors otherwise make latency look better during an outage.
- Distributions, not averages: is latency a histogram or summary, not a mean gauge?
- Histogram buckets: do they extend past the call's timeout? If the top bucket is below the timeout, every slow request lands in the overflow bucket and the tail is invisible.
- Metric types: counters for totals, gauges for levels; a counter that is reset or decremented breaks rate math.
- Renames: does a renamed metric or log field still match the alerts, dashboards, and queries that use it? Grep the old name.
- Trace context: is it carried across the new queue message or thread hop, or does the trace end there?
- Error classification: are dependency failures distinguishable from caller errors in the emitted signal?
- Absence: does a new periodic job emit a heartbeat or last-success timestamp so silence is detectable?

## Findings vs questions

- Finding: duration recorded only after the success branch (quote it), bucket bounds that stop below the configured timeout (quote both), an old metric name still referenced by an alert after the rename.
- Question: whether instrumentation exists in a shared middleware the diff does not show.

## Not a finding

- Missing metrics in a library that leaves instrumentation to callers.
- Log-level and cardinality items the main rubric already covers.
