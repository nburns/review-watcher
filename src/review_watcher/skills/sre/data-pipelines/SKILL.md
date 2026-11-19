---
name: data-pipelines
description: "Use when the diff adds or changes batch jobs, ETL or ELT steps, stream consumers, scheduled aggregations, exports, backfills, offset or checkpoint handling, or the output schema other jobs read."
sources:
  - "SRE Book Ch25"
  - "Workbook Ch13"
  - "google-sre-skills (MIT): data-pipelines"
---

## Applies when

- A job reads a dataset or stream and writes derived output.
- Offsets, checkpoints, watermarks, or windowing change.
- Output columns, partitioning, or write mode change.

## Questions to ask of the diff

- Completeness: does a per-record error handler log and continue? Then the job reports success while dropping records. Is there a skipped-record counter, and does the job fail past a threshold?
- Input versus output counts: does anything compare them?
- Freshness: does the job record a last-success time or watermark that can be alerted on, beyond its exit code?
- Late and out-of-order data: what happens to records that arrive after their window closes? Are day boundaries computed in a consistent timezone?
- Replay: can the run for a given period be repeated without duplicate output? Overwrite a partition, or append?
- Checkpoint order: is the offset committed before the side effect (loss on crash) or after (duplicates, so the write must be idempotent)?
- Skew: is the partition or grouping key low-cardinality or dominated by one tenant?
- Upstream schema change: a renamed or missing field fails loudly, or becomes nulls downstream?
- Memory: does the job load an entire table or file instead of streaming?
- Consumers: grep for readers of the output before changing its schema.

## Findings vs questions

- Finding: a catch-and-continue in a record loop with no counter (quote it), an offset commit before processing, append mode in a job described as rerunnable, a full-table load into memory.
- Question: freshness and completeness targets, and who consumes the output when the readers live outside the repo.

## Not a finding

- One-off scripts clearly marked as such.
- Small, fixed-size reference datasets loaded whole.
- Queue lifecycle items the main rubric already covers.
