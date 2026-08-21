# FrameFactory Worker

The worker consumes API run wake-ups from the shared Redis `runs` queue,
materializes the immutable PostgreSQL pipeline graph into durable `run_steps`,
then executes the `run-steps` queue with compare-and-swap state transitions.

Apply `migrations/0001_runtime_state.sql` after the root database migrations,
install this package, and configure the variables shown in `.env.example`.

```shell
python -m framefactory.worker healthcheck
python -m framefactory.worker run
```

Production mode fails closed unless PostgreSQL and Redis use TLS, or
`FRAMEFACTORY_ALLOW_INSECURE_TRANSPORT=true` is explicitly set for a trusted
private network. Missing research, model, speech, media, render, and delivery
providers are persisted as permanent capability errors. The worker never emits
a placeholder artifact or reports a fabricated video success.

## Execution providers

`research.collect`, `writing.compose`, and metadata-only `quality.evaluate` can
use any configured OpenAI-compatible `/chat/completions` endpoint with strict
JSON-schema responses. Provider URL, model names, and key come only from
environment variables; no account or vendor ID is embedded in a Skill or step.
Secrets support `FRAMEFACTORY_OPENAI_API_KEY_FILE`,
`FRAMEFACTORY_S3_ACCESS_KEY_ID_FILE`, and
`FRAMEFACTORY_S3_SECRET_ACCESS_KEY_FILE` for Docker/Kubernetes secret mounts.
Direct and `_FILE` values are mutually exclusive, and empty secret files stop
startup.
Timeout/network failures, 408/409/425/429, and 5xx responses are retryable.
Provider rejection and malformed structured output are permanent failures. Error
messages never include request bodies, response bodies, credentials, or URLs.

Text outputs are content-addressed, conditionally uploaded to the configured
S3/R2-compatible bucket, recorded in the `artifacts` table, and then referenced
from the durable step. A retry verifies the existing object hash. The worker
will not call a model when durable artifact storage is missing.

Run materialization joins the immutable published SkillVersion and captures its
research/writing/visual/asset/QC policies plus the composition snapshot under
`_framefactory` in every step input. This prevents provider execution from
silently ignoring the published Skill configuration.

TTS, asset acquisition/generation, and rendering use the explicit
`SpeechProvider`, `AssetProvider`, and `RenderProvider` plugin boundaries in
`framefactory.worker.providers`. No production plugins are bundled yet, so
`audio.synthesize`, `media.select`, `media.generate`, `timeline.align`, and
`render.compose` fail closed with `CapabilityUnavailable`. Consequently this
release can persist provider-produced research-brief and script artifacts but cannot claim a
finished video. Metadata-only QC always requests human review and can run only
when a real video artifact was produced by a future render plugin.

Redis is the low-latency wake-up path. Startup and periodic PostgreSQL scans also
materialize queued runs with no steps, so a process crash between API commit and
Redis publish cannot lose work. Duplicate jobs and recovered leases remain safe
through Redis lease fencing and PostgreSQL `worker_revision` CAS writes.

Control integrations can call `WorkerService.cancel_run(...)` and
`WorkerService.review_step(...)`. Cancellation writes `runs.cancel_requested_at`
and is observed at step checkpoints. Review decisions transition the durable
step, append `review_actions`, and enqueue downstream/retry work as appropriate.

## Quarantined asset analysis

Apply root migration `0010_asset_analysis_pipeline.sql` before using the asset
job tool. The pipeline leases each asset independently and checkpoints file
detection, malware scanning, ffprobe validation, SHA-256 verification,
keyframes, configured visual analysis, shot segmentation, normalized tags, and
the source/license gate. A process restart skips checkpoints already committed
for the same immutable source hash. Failed items retry independently and do not
stop their batch.

Candidate selection is read-only and defaults to a 10-item canary (hard maximum
100):

```shell
python tools/asset_jobs.py --database-url postgresql://... plan \
  --workspace-id 00000000-0000-0000-0000-000000000000
```

Mutating commands in this release are intentionally locked to database names
containing `test` and require `--confirm-test-data`. They never fetch media from
internet URLs; media must already exist in the configured object store. Running
a canary also requires FFmpeg/ffprobe, an externally managed malware scanner
(ClamAV by default), and an explicitly authorized visual-analysis command using
JSON over stdio. Successful analysis always ends at `awaiting_review`;
`auto_ready` is false. Approval still fails closed until copyright is one of
`owned`, `licensed`, or `public_domain`, the scan is clean, and a completed
analysis exists. Unknown copyright can never be approved without first
resolving its provenance and copyright record.
