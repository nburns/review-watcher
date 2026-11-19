---
name: release-engineering
description: "Use when the diff changes build scripts, CI pipeline definitions, container build files, dependency manifests or lockfiles, version stamping, artifact publishing or tagging, or release and hotfix workflows."
sources:
  - "SRE Book Ch8"
  - "SRE Book 2e Ch14"
  - "google-sre-skills (MIT): release-engineering"
  - "agent-skills-reliability (MIT): deployment-safety"
---

## Applies when

- CI or build pipeline files, container build files, or release scripts change.
- Dependency manifests or lockfiles change.
- Artifact naming, tagging, or publishing changes.

## Questions to ask of the diff

- Reproducibility: unpinned base images (latest or a floating major tag), dependency ranges with no lockfile, scripts that download and execute from the network at build time, builds that read the current date or host state.
- Manifest and lockfile: does a manifest change come with the matching lockfile change?
- Build once, promote: does the pipeline rebuild per environment, baking environment values into the artifact, instead of promoting one artifact through environments?
- Version identity: is the artifact stamped with the commit so logs and metrics can name the running version?
- Rollback: are tags immutable? Re-pushing an existing tag makes "roll back to that tag" deploy the new code. Is the previous artifact kept?
- Gates: does the diff weaken a gate, for example a test step allowed to fail, an error swallowed with a shell fallback, a required check removed, or hooks skipped?
- Hotfix path: does an emergency workflow bypass tests or review entirely? It should be faster, not unchecked.

## Findings vs questions

- Finding: a test or lint step made non-blocking (quote the line), a floating image tag in a production build, a publish step that overwrites an existing version tag.
- Question: whether promotion and retention happen in tooling outside the repo.

## Not a finding

- Floating tags in local-dev or devcontainer files.
- Routine lockfile churn from a dependency bump.
- Build-speed or caching changes with no effect on output.
