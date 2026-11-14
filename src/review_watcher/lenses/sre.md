<!--
Lenses adapted from https://github.com/paulip114/sre-practice-skills (sre-release-and-launch,
sre-monitoring-and-alerting, sre-data-reliability), commit 3f0545a1, Apache-2.0. Finding grounding
after https://github.com/gregoryroot/project-review (d58b0519, MIT) and the production-readiness-review
skill in https://github.com/Security-Phoenix-demo/security-skills-claude-code (40ace4aa, MIT), via
the exp-core sre-review skill. Licences in LICENSES/. Changes: condensed and rewritten as generic
PR-time questions for any repository; repository-specific alarms, components and docs removed.
-->
Operational lens (built in, applies alongside the built-in checks):

Judge what this change does to the running system, not only whether the code is correct.

Routing:
- First look for the repository's own operational docs and use them as the source of truth for
  the rules: a system or service map (for example docs/system-map.yaml), ARCHITECTURE, SCALING,
  OBSERVABILITY, runbooks, deploy or migration rules, and the AGENTS.md or CLAUDE.md of each
  changed directory. Do the same in a related repository when the change calls into it or is
  deployed by it.
- Map each changed path to the component or service it belongs to, then to what it depends on and
  what depends on it. Trace in execution order: entrypoint, handler, outbound call, the config that
  sets its limits, the infrastructure that provisions the dependency, and the alert on it.
- Weight effort by risk: request paths, auth, data writes, migrations, queues and infrastructure
  get a full pass; a change with no runtime effect gets one line in the summary saying so.
- When the change alters a timeout, retry, cache TTL, pool size, health check, alert, queue setting
  or adds an outbound call, and the repository documents those values somewhere, the doc should
  change with it.

1. Failure handling - every outbound call the change adds or alters (HTTP, SDK, DB, cache, queue):
- Is there a timeout, shorter than the caller's own deadline?
- Is retry bounded, with backoff and jitter, and only around idempotent work? A retry inside a
  request that is itself retried multiplies load on a struggling dependency.
- Is the failure behaviour chosen and visible: fail closed, serve stale, degrade, or retry later?
  A silent fallback that looks like success, such as an empty list for a failed fetch, is major.
- Does it reuse the pattern a sibling already uses instead of inventing a second mechanism?
- When the dependency is slow rather than down, what does the caller hold while it waits (a worker,
  a connection, a request slot, a lock)? Slow is the expensive failure.

2. Blast radius:
- Does the change add a hard dependency to a critical path (login, auth, checkout, the main request
  path) or a call inside a health check? A health check that calls another service turns that
  service's outage into this one's.
- Does it put tenants or customers on one shared finite resource (a queue, a pool, a quota) so one
  burst becomes everyone's outage? Name the resource and its limit.
- Does it introduce a single point of failure, or cross-region or cross-environment coupling?
- Does monitoring depend on the thing it monitors?

3. Capacity:
- Connections: pool size times max instances, doubled during a rolling deploy, against the
  database's connection limit, for every process type together.
- Shared quotas and rate limits a new consumer will draw on.
- Autoscaling bounds and what scaling out does to the dependency behind it.
- Bursts: polling, bulk sends, backfills, onboarding a large customer. Is there a backlog, rate
  limit, or batch size, and is it observable?
- Unbounded work: queries without limits, loops over all tenants or rows in a request or cron tick.

4. Detectability - for each failure mode the change introduces: who notices first, and how fast?
- Logs carry a request or job id, the decision and its reason, at the right level; expected client
  errors are not logged as errors; secrets, tokens and personal data are never logged.
- Metrics use low-cardinality labels, never a tenant, user, host or id.
- An alert exists on the symptom (error rate, latency, backlog age) and says what users see and the
  first diagnostic step. A metric that stops being emitted should page, not go quiet.
- Unexpected errors reach the error tracker with context before they are swallowed or retried.
- "A customer reports it" as the first detector for a failure on a critical path is a finding.

5. Health semantics:
- Health checks test only what the service cannot work without, cheaply, and never another service.
- Liveness and readiness are not conflated; degradation is an alert, not a failed health check.

6. Deploy and rollback safety:
- Overlap: old and new versions run together during a deploy. Old code against the new schema, new
  code against old data, old instances against new config or infrastructure must all work.
- Migrations are online (no long locks or table rewrites on large tables, nullable adds first,
  multi-step constraint changes), and backfills are batched and idempotent.
- Sequencing across services and repositories is stated: what must deploy first, and what an
  out-of-order step leaves running.
- New config is validated at startup and rejects empty or missing values rather than treating them
  as "all" or "none"; it is set in every environment that runs the code.
- Rollback: what is the one-step undo? A migration or data rewrite without a backward path should be
  stated in the PR.

7. Worker and process lifecycle:
- Shutdown: stop taking new work, finish or release in-flight work within the stop timeout.
- Queues: visibility or ack timeout longer than worst-case processing, a dead-letter path with a
  receive limit, and alerts on dead-letter depth and oldest-message age.
- Poison messages are quarantined, not retried forever, and do not block the queue behind them.
- Handlers are idempotent under at-least-once delivery; outputs are written atomically.
- Scheduled work cannot double-run or silently skip; a failure for one tenant does not stop the rest,
  and partial failures are visible.

8. Telemetry cost:
- Log lines per request on hot paths, debug logging left on, payloads logged whole.
- Trace sampling set per environment, health checks excluded.
- Every new label value is a new metric series; per-tenant or per-route alerts are cardinality in
  disguise.

Reporting operational findings:
- Use category reliability for failure handling, blast radius, capacity, health, deploy, rollback
  and lifecycle findings, and observability for detectability and telemetry findings.
- A finding that something is missing (no timeout, no alert, no dead-letter queue) states the search
  that came back empty in its evidence and quotes the call or config that lacks it.
- A concern you cannot ground in the code goes in questions_for_author, not in findings.
