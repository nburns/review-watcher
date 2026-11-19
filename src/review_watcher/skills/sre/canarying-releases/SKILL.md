---
name: canarying-releases
description: "Use when the diff changes rollout strategy or deploy manifests, traffic split or flag percentage ramps, canary analysis or abort thresholds, bake times, or serialized formats that canary and baseline versions both read."
sources:
  - "Workbook Ch16"
  - "SRE Book 2e Ch14, Ch17"
  - "google-sre-skills (MIT): canarying-releases"
  - "agent-skills-reliability (MIT): deployment-safety"
---

## Applies when

- Deploy or rollout config, stage lists, or traffic weights change.
- A flag ramp percentage or targeting rule changes.
- Canary analysis metrics, thresholds, or bake durations change.
- A cache entry, message, or file format written by the service changes.

## Questions to ask of the diff

- Does the rollout go through stages, or straight to 100 percent? Read the step list or surge and unavailable settings.
- Abort criteria: does analysis compare canary against baseline on user-facing error rate and latency, or only check that instances are running?
- Bake time: long enough to cover a traffic peak or the scheduled jobs the change touches? A few seconds on a critical path proves little.
- Does the canary exercise the new code? If the feature ships behind a flag that is off, a binary canary tests nothing about the feature; the flag ramp is the real canary.
- Config and data changes: does the diff ship a config or data change that reaches all instances at once, outside the binary canary?
- Shared state contamination: canary and baseline share caches, queues, and tables. Can baseline read what the canary writes (new cache shape, message schema, enum value)? A bad canary can poison shared state for everyone.
- Is rollback automatic on a failed analysis, or does it need someone to notice?

## Findings vs questions

- Finding: a rollout config that sends full traffic in one step (quote it), a new serialized field or enum value with a reader in the old code path that would reject it (quote both).
- Question: abort thresholds and bake times when the analysis config lives outside the repo.

## Not a finding

- Libraries and tools with no deploy config in the repo.
- Repos whose deploy process is documented elsewhere; ask instead of assuming a big-bang release.
