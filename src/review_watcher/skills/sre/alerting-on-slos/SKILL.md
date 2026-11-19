---
name: alerting-on-slos
description: "Use when the diff adds or changes alert rules, SLO or SLI definitions, recording rules, burn-rate windows, alert routing or severity, or renames a metric that alert expressions reference."
sources:
  - "Workbook Ch5"
  - "SRE Book Ch10"
  - "google-sre-skills (MIT): alerting-on-slos"
  - "agent-skills-reliability (MIT): alert-design"
  - "sre-practice-skills (Apache-2.0): sre-slos-and-error-budgets"
---

## Applies when

- Alert rule, SLO, or recording rule files change.
- Notification routing, severity, or alert annotations change.
- Code renames or relabels a metric that an alert might use.

## Questions to ask of the diff

- Does the alert expression reference metric names and labels that the code actually emits? Grep the codebase for each name in the expression. A rename on either side silently disables the alert.
- Ratio SLIs: are good and valid events from the same source with matching labels? With zero traffic the ratio is undefined; does the rule then go silent, and is no-data handled?
- Burn rate: is the alert on error budget consumption rather than a raw threshold? Is a long window paired with a short window (roughly a twelfth of it) so the alert both confirms and resets quickly?
- Severity split: fast burn pages, slow burn opens a ticket. Does each page carry user-impact text and a runbook link like its siblings?
- Low traffic: is there a minimum event count so a few failures do not page?
- Hold duration: is the pending period so long that budget is spent before it fires, or so short that it flaps?
- Consistency: does the threshold match the target in the SLO definition file, or is it a copy that drifted?
- Ownership: is the route a real team destination, not a placeholder?

## Findings vs questions

- Finding: an expression naming a metric no code emits (quote it, state the empty search), a page rule missing the runbook annotation its siblings carry, a threshold that contradicts the SLO target in the repo (quote both).
- Question: whether the chosen target and windows fit the service's traffic and history.

## Not a finding

- Specific burn multipliers or windows that differ from textbook values.
- Cause-level alerts routed to tickets or dashboards, not pages.
