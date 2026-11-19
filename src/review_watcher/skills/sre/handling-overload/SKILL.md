---
name: handling-overload
description: "Use when the diff adds or changes in-memory queues or buffers, worker or concurrency limits, rate limiters, request admission, priority classes, bulk endpoints, or how a client or server responds to being overloaded."
sources:
  - "SRE Book Ch21"
  - "SRE Book 2e Ch15, Ch17"
  - "Workbook Ch11"
  - "google-sre-skills (MIT): handling-overload"
  - "sre-practice-skills (Apache-2.0): sre-capacity-and-system-design"
---

## Applies when

- A queue, channel, buffer, or executor is created or resized.
- A rate limit, semaphore, concurrency cap, or batch size is added or changed.
- An endpoint does work proportional to input size (bulk, export, search).
- A client is added that calls a service which can return 429 or 503.

## Questions to ask of the diff

- Is every new queue or executor bounded? Grep the constructor for a max size. What happens when it is full: block the caller, drop, or reject with an error?
- Is rejection cheap and early, before parsing large bodies, taking locks, or querying the database?
- Does overload produce a distinct, retryable-later signal (429 or 503 with a retry hint), rather than a 500 or a 200 with an empty body?
- Does background, batch, or prefetch work share the same pool or limit as user-facing requests? Can it be shed first?
- Rate limiter scope: is it per instance (real limit = limit times instance count) or shared? Is it keyed per tenant, or can one tenant consume it all?
- Does the new client back off on 429 and 503 and stop retrying once a retry budget is spent?
- Does a worker check whether a queued item's caller has already timed out before processing it?
- Is there a cheaper degraded response (cached, partial) and can it be switched on without a deploy?

## Findings vs questions

- Finding: an unbounded queue or executor on a request path (quote the constructor), an overload or failure path that returns success, a limiter documented as global but implemented in process memory.
- Question: whether expected traffic approaches the chosen limit, and whether a load test showed behavior past it.

## Not a finding

- Unbounded collections in tests or in code with a bounded producer.
- Admin or internal endpoints with a handful of callers.
- A limit value chosen without data; ask how it was chosen instead.
