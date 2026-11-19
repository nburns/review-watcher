---
name: simplicity
description: "Use when the diff adds a new abstraction layer, wrapper, dependency, service, config knob, or flag-guarded dual path, or removes a feature but may leave its config, jobs, or alerts behind."
sources:
  - "SRE Book Ch9"
  - "Workbook Ch7"
  - "google-sre-skills (MIT): simplicity"
  - "sre-practice-skills (Apache-2.0): sre-capacity-and-system-design"
---

## Applies when

- A new client wrapper, helper layer, or internal framework appears.
- A new package or service dependency is added.
- A flag keeps old and new implementations both live.
- A feature or endpoint is removed.

## Questions to ask of the diff

- Second mechanism: does the diff add its own retry, cache, queue, HTTP client, or config loader when the repo already has one? Grep for the existing one. Two mechanisms means two sets of failure modes and tuning.
- Wrappers that hide failure: does a new layer catch exceptions and return a default, making failures invisible to callers and monitoring?
- New knobs: is every new option set somewhere other than its default? An option nobody sets is untested surface.
- Dual paths: when both old and new paths live behind a flag, is there a removal plan or note?
- New dependency: does it pull in a large package or a new runtime service for a small function?
- Leftovers: after a removal, grep for the removed name in config, cron or scheduler files, alert rules, and docs. Orphaned jobs keep running and orphaned alerts page for nothing.
- Explainability: can the operational behavior of the change be stated in a sentence? If not, ask the author for it.

## Findings vs questions

- Finding: a duplicate mechanism next to an existing one (quote both), an exception-swallowing wrapper (quote the handler), a removed job still scheduled in config (state the search).
- Question: why a new dependency or layer is needed when an existing one appears to fit.

## Not a finding

- Style or naming preferences.
- Abstractions with several real callers.
- Complexity that the problem itself requires.
