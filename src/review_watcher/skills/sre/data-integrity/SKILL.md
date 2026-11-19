---
name: data-integrity
description: "Use when the diff deletes or bulk-updates data, changes retention or TTLs, adds a datastore, table or bucket, changes a serialization format or type conversion, or adds cleanup jobs or read-modify-write logic."
sources:
  - "SRE Book Ch26"
  - "SRE Book 2e Ch16"
  - "google-sre-skills (MIT): data-integrity"
  - "agent-skills-reliability (MIT): production-readiness-review"
---

## Applies when

- Delete, truncate, purge, or bulk update statements or calls appear.
- Retention, TTL, lifecycle, or archival settings change.
- A new store, table, or bucket is created.
- Encoding, schema, or type conversion of persisted data changes.

## Questions to ask of the diff

- Empty filter: can the condition on a delete or update be built from input that might be empty or missing? An empty filter often means all rows.
- Soft delete: is there a recovery window (tombstone, delayed purge), or is the delete immediate and final?
- Retention: does a shorter TTL or lifecycle rule delete existing data on deploy? Is that stated in the PR?
- Round trip: can old readers read new data and new readers read old data? Look for lossy conversions: float precision, dropped timezone, truncation to column length, enum values the old code rejects.
- Lost updates: read, modify, write back without a transaction, version check, or conditional write.
- Cleanup jobs: how does the job decide something is orphaned? Can it race with an object being created right now?
- Backups: is a new store or table covered by the backup config in the repo, or excluded by a pattern?
- In-place rewrite: does the change overwrite original values, or copy then swap so the original survives?

## Findings vs questions

- Finding: a delete whose filter can be empty (quote it), a lossy cast on a persisted field, a read-modify-write with no concurrency guard, a new table missing from a backup list that names its siblings.
- Question: backup coverage and restore testing when they are configured outside the repo.

## Not a finding

- Deletes in test fixtures or seed scripts.
- Dropping a column that grep shows is unread, in a planned contract step.
- Retention changes that the PR description explicitly calls out.
