---
name: testing-for-reliability
description: "Use when the diff adds risky behavior (outbound calls, migrations, flags, retries, config parsing) and its tests, or changes tests by skipping, loosening, or mocking them, or changes test selection in CI."
sources:
  - "SRE Book Ch17"
  - "SRE Book 2e Ch14"
  - "google-sre-skills (MIT): testing-for-reliability"
  - "agent-skills-reliability (MIT): deployment-safety"
---

## Applies when

- New error handling, retries, fallbacks, or migrations land with or without tests.
- Tests are skipped, marked expected-to-fail, deleted, or have assertions relaxed.
- Mocks or fakes of a dependency change.
- CI test selection or thresholds change.

## Questions to ask of the diff

- Failure paths: for a new outbound call, are there tests for timeout, error status, and malformed response, not only success?
- Mocks that cannot fail: does the fake only ever return success? Then the error handling is untested.
- Flag states: are both on and off paths tested, including the off path after data was written by the on path?
- Migrations: tested forward on representative data, not only an empty schema? Is the backward step tested if one exists?
- Config: is startup tested with the new key missing, empty, and out of range?
- Idempotency: is a handler that claims idempotency run twice in a test?
- Weakened checks: grep the diff for skip markers, expected-failure markers, commented-out asserts, raised tolerances, or longer test timeouts. Each needs a reason.
- Time: do tests sleep or read the wall clock, making them flaky under load?
- CI: does a selection change stop running a suite that covers the changed code?

## Findings vs questions

- Finding: a test skipped or an assertion loosened in this diff with no reason given (quote it), a fake patched to swallow errors, a CI path filter that excludes the changed directory.
- Question: whether integration or failure-injection coverage exists outside the repo.

## Not a finding

- Missing tests for trivial or declarative changes.
- Asking for chaos or load tests as a PR requirement.
- Test style or structure preferences.
