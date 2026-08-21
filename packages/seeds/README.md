# FrameFactory seed packages

Seed packages are curated, declarative bootstrap data. They are not Worker
plugins and they never receive executable privileges.

`official-skills/v1` contains the first-party starting points migrated from a
small, representative subset of the legacy `src/ip-skills` library. Official
status is data only: each `skill.json` uses `ownership_type=system` and
`publisher_type=system`; the corresponding `version.json` still conforms to
the same `SkillVersion` contract used by user-created versions.

Every official SkillVersion references the same packaged `standard-production`
PipelineVersion. Its declarative graph runs research, writing, audio and media
selection, rendering, then quality evaluation with a human review gate. It is
validated like any other Pipeline and grants no executable privileges.
The Pipeline hash covers `nodes` and `capability_requirements` after the same
RFC 8785 JCS canonicalization used by SkillVersion hashes.

Important boundaries:

- `_research`, raw corpora, `corpus_ref`, generated runs, and media are not seed
  inputs and must never be copied into this directory.
- Writing guidance is a short, manually curated method summary. The legacy
  formula and usage documents remain migration evidence, not production seed
  payloads.
- Legacy paths and business IDs live only in
  `../tools/migrate-v1/legacy-sources.json`; they are deliberately absent from
  this production seed package.
- IDs are UUIDv5 values derived from the namespace and key format declared in
  the package manifest. No runtime code may branch on these IDs.
- `content_hash` is SHA-256 over the exact immutable fields named by the
  `SkillVersion` contract after RFC 8785 JCS canonicalization. This package
  intentionally uses the integer-only numeric subset so the bundled validator
  can reproduce JCS without a third-party canonicalizer.

Validate and print an idempotent database import plan (dry-run only):

```powershell
python ../tools/migrate-v1/skill_seed_plan.py
```

Use `--output <path>` only when a persisted review artifact is required. The
tool has no database driver, no apply mode, and never moves legacy files.
