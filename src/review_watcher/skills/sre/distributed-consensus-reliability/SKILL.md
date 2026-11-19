---
name: distributed-consensus-reliability
description: "Use when the diff adds or changes leader election, distributed locks or leases, run-on-one-instance logic for scheduled work, compare-and-swap coordination, or the member count and placement of a quorum-based store."
sources:
  - "SRE Book Ch23"
  - "google-sre-skills (MIT): distributed-consensus-reliability"
  - "sre-practice-skills (Apache-2.0): sre-capacity-and-system-design"
---

## Applies when

- Code acquires or releases a lock, lease, or leadership.
- A scheduled job assumes only one instance runs it.
- Replica or member counts of a coordination or consensus-backed store change.
- Correctness depends on reading the latest write.

## Questions to ask of the diff

- Home-made election: is leadership decided by a heartbeat row or timestamp ("leader if last seen more than N seconds ago")? Clock skew and long pauses give two leaders. A proven consensus-backed primitive is safer.
- Lease length: is the lock TTL shorter than the worst-case work under it, including the configured timeouts of calls inside? If the lease expires mid-work, two holders run.
- Fencing: does the protected write check a token or version from the lock, so a stale holder's late write is rejected?
- Release: is the lock released on every error path, and only by its owner (delete conditioned on owner id)?
- Single-run jobs: with several replicas, what stops each from running the job? Grep for the lock around the job entrypoint.
- Quorum size and placement: an even member count tolerates no more failures than one fewer. Are members in distinct failure domains, without putting a majority far apart and paying cross-region latency on every write?
- Store misuse: is bulk data or a high write rate going into the coordination store instead of pointers and metadata?
- Stale reads: are reads served from followers or caches where the caller needs the latest value?

## Findings vs questions

- Finding: an unconditional lock delete (quote it), a lease TTL shorter than a timeout used inside the critical section (quote both), an even quorum member count, a scheduled job with no lock and multiple replicas in the manifest.
- Question: the consistency the feature actually requires, when the code does not say.

## Not a finding

- Correct use of an established lock or election library with fencing.
- Single-node development or test setups.
