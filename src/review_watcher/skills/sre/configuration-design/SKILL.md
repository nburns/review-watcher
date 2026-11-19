---
name: configuration-design
description: "Use when the diff adds or changes config keys, env vars, defaults, feature flags, per-environment overrides, config parsing or validation, or dynamic config reload."
sources:
  - "Workbook Ch14, Ch15"
  - "google-sre-skills (MIT): configuration-design"
  - "agent-skills-reliability (MIT): deployment-safety"
---

## Applies when

- A config key, env var, or flag is read for the first time or its default changes.
- Config schema, parsing, or reload code changes.
- Values differ between environment overlays in the same diff.

## Questions to ask of the diff

- Default on absence: what does the code do when the key is missing? Check that the default is the safe side. Classic traps: an empty allowlist meaning allow all, a missing limit meaning unlimited, 0 meaning no timeout in many clients.
- Units: does the key name carry its unit (timeout_ms, ttl_seconds)? Grep every reader of the key; do they agree on the unit?
- Ranges: does validation check bounds, not only type? A percentage above 100, a negative TTL, a pool size of 0.
- Parsing: does a string "false" become truthy? Does an unparseable number fall back silently?
- Dynamic reload: is a bad new value rejected while the last good one stays active, or does the process crash or adopt it? Does a change reach every instance at once, or is it staged?
- Blast radius: does one key now control all tenants or regions? Can it be scoped?
- Flags: is there a removal note or owner for a temporary flag? Do flags nest so that combinations are untested?
- Is a kill switch added here documented where operators will find it?

## Findings vs questions

- Finding: a default that disables a safety limit (quote the read), two readers of one key using different units (quote both), a boolean parsed with a truthiness check on a string.
- Question: how a global config change will be rolled out, when the rollout tooling is outside the repo.

## Not a finding

- Defaults in test fixtures or local-dev files.
- Startup validation and per-environment presence; the main rubric covers those.
- Missing flag expiry in a repo with no flag lifecycle convention; ask at most.
