# Vistora product engineering readiness matrix

Assessment date: 2026-09-04
Decision: **not production-ready without the target-environment release gates**

This matrix describes implemented behavior in this repository. A passing unit or
in-process integration test is not represented as proof that an external provider,
identity gateway, backup, or deployed worker is healthy.

## Product surfaces

| Surface | User path | Control plane | Worker / provider path | Recovery and honest states | Evidence status |
|---|---|---|---|---|---|
| Standard video | `/create` | Durable Run, Pipeline and Skill snapshots | research, writing, TTS, retrieval, timeline, FFmpeg, QC | idempotent create, retries, cancellation, review | Implemented; provider E2E is environment-gated |
| Full-AI video | `/create/ai` | Dedicated quote/options/create endpoints and paid-operation ledger | governed generation candidates, verification, selection, timeline/render | fail-closed blockers, capped quote, retry-safe paid operations | Implemented but official Skill remains archived; real provider acceptance is required |
| Webpage video | `/create/webpage-video` | Dedicated URL capture resource, attempt/evidence APIs | isolated capture Worker, screenshot review, writing/render | SSRF policy assertion, retry, cancellation, human capture review | Implemented; production egress proxy and browser isolation must be proven |
| PDF document video | `/create/document-video` | Immutable PDF upload, hash/size completion, dedicated Run creation; retention, legal-hold and purge APIs | Poppler inspection/extraction, evidence storyboard, page rendering, TTS, timeline, composite render, QC; leased S3 purge worker | source/pipeline review gates, retry with same key, run cancel/retry/download, fail-closed scanned PDF; purge retries and tombstones | Implemented in code; protected external-HTTP acceptance and purge integration still require a running release stack |
| Application demo | `/create/application-demo` | Uses governed production Run contracts | Reuses production video capabilities | exposes failures through Run UI | Implemented composition surface; no separate renderer |
| Broadcast revival | `/create/broadcast-revival` | Uses durable asset/library and Run contracts | archive-first retrieval and production renderer | missing evidence fails closed | Implemented bounded workflow; external archive rights remain operational responsibility |
| Projects / review | `/projects`, `/runs/:id` | Run, step, artifact, event and review APIs | queue state and persisted artifacts | loading/error/empty, cancel, retry, approve/reject/request changes, downloads | Implemented and covered by Web/API tests |
| Batch production | `/batches` | durable batch endpoints and pagination | fan-out to Run queue | persisted progress and per-item failure | Implemented; load/capacity test is still required |
| Assets | `/assets` | immutable object records, metadata, status, analysis jobs | S3, analysis and retrieval | quarantine, optimistic revision, idempotent jobs | Implemented; restore/integrity gates are environment-dependent |
| Skills / Pipelines | `/skills` | schema validation, canonical hashes, publishing and tests | declarative operation registry | invalid or unavailable capability is rejected | Implemented; new document Pipeline is active and capability-gated |
| Channels | `/channels` | tenant-scoped CRUD and optimistic concurrency | defaults feed Run composition | archive and stale-revision handling | Implemented and API-tested |
| Account settings | `/settings` | preferences and API-key metadata | not a request-auth boundary | ETag/revision conflict states; secrets are not read back | Implemented as settings; production identity must be supplied by a trusted gateway |

## Engineering controls

| Area | Implemented evidence | Status / residual risk |
|---|---|---|
| API contracts | JSON Schema, OpenAPI, Pydantic validation, canonical seed hashes | Pass in local contract suite |
| Tenant isolation | workspace-scoped repository methods plus PostgreSQL RLS, including `document_sources` | Code-reviewed and unit-tested; target PostgreSQL role/RLS proof remains required |
| Input and source safety | PDF basename/type/size/hash/rights contract; full-byte S3 SHA-256 verification; Worker rejects encryption/JavaScript and >100 pages | Pass in code/tests; antivirus/CDR and OCR are not implemented |
| Prompt / content injection | document instructions are treated as untrusted evidence and never executed; no PDF links are fetched | Implemented for document flow; adversarial corpus testing remains |
  | Idempotency and concurrency | source/run/purge idempotency, queue deduplication, optimistic revisions, paid-operation ledger; purge `SKIP LOCKED` leases, upload-grant expiry fence and locked artifact-manifest recheck | Covered by API/Worker tests; distributed fault injection remains |
| Cancellation / retries | durable step leases, checkpoints, bounded retries, cancel endpoints | Covered by Worker/API suites; crash test against a live queue remains |
| Artifact integrity | immutable hashes, workspace object prefixes, server-resolved document source snapshot; source/derived version purge before DB tombstones | Implemented; S3 versioned-bucket purge and integrity gates must run against deployment |
| Human review | document storyboard and final QC always stop at review; webpage capture review is persisted | Implemented; reviewer staffing/SLA is operational |
| Provider cost controls | full-AI quote/caps/ledger; document Agnes call is not attempted without a governed adapter | Fail-closed; Agnes currently uses a disclosed procedural fallback |
| Performance / capacity | bounded PDF bytes/pages, extraction truncation, provider timeouts, container limits | Bounds exist; no production concurrency or 200 MiB PDF load result |
| Observability | run/step events, structured Worker state, health checks and release tools | Implemented baseline; alert routing and dashboards require deployment evidence |
| Database change safety | forward, transactional checksum migrations including `0026` document control and `0027` retention/purge | Migration unit tests pass; live migration was blocked when Docker became unavailable |
| Backup / restore | documented PostgreSQL/S3 strategy and dedicated release gates | Not verified in this workspace; release blocker for production |
| Supply chain | pinned production requirements/hashes, pinned CI actions and container tags | Full Python/npm dependency gate passed locally after upgrading `pypdf` and `fast-uri`; image/SBOM policy still requires release execution |
| CI / release | API, Worker, Web, contracts, security, document-video E2E, recovery, S3 and asset gates | Static security and asset contract gates passed; protected document-video/recovery gates remain blocked by missing release targets |

## Current release blockers

1. **P1 — target deployment proof is absent.** Run the security, API E2E,
   recovery, PostgreSQL restore, S3 integrity, and asset contract gates against an
   isolated release candidate. Any `BLOCKED` result is not a pass.
2. **P1 — document browser-to-video acceptance is unverified.** Exercise a legally
   usable, text-layer PDF through Web upload, S3, PostgreSQL, Redis, Worker, both
   review gates, and final MP4 download. The protected
   `tools/release/document_video_e2e_gate.py` now verifies the external API,
   storage and media path fail-closed, but the local Docker engine and approved
   fixture were unavailable during closing verification; a browser UI smoke is
   still separately required.
3. **P1 — production identity and outbound policy are deployment controls.** Prove
   the trusted identity gateway, tenant claims, browser-capture egress proxy,
   restricted capture credentials, TLS, and alerting on the target platform.
4. **P1 — document purge has no live PostgreSQL/S3 acceptance evidence.** Migration
   `0027` and the API/Worker workflow now freeze the source, reject active retention,
   legal holds and active Runs, lease work with `SKIP LOCKED`, delete every S3 object
   version, and commit sanitized tombstones while retaining Run/review/audit lineage.
   The live versioned-bucket, crash/retry and least-privilege credential paths remain
   unverified while Docker is unavailable; the Web has no retention administration
   panel yet, so this is an API/operator control rather than a complete self-service UI.
5. **P2 — document coverage is intentionally bounded.** Scanned-only PDFs fail
   with an explicit OCR-not-configured error, Agnes is a disclosed procedural
   fallback, and automatic evidence-legibility scoring is not implemented.
6. **P2 — capacity evidence is absent.** Benchmark concurrent long documents,
   object storage throughput, Worker disk/memory, queue age, and provider budgets.

## Required production acceptance

- Use a dedicated release environment and real provider accounts with hard spend
  caps; do not substitute fixtures for provider acceptance.
- Upload at least: a normal text PDF, encrypted PDF, JavaScript-bearing PDF,
  scanned-only PDF, near-200 MiB PDF, and cross-tenant source identifier.
- Interrupt API, Worker, Redis, and object storage at defined points and verify
  idempotent recovery, cancellation, artifact integrity, and no duplicate paid call.
- Complete both human-review decisions and verify the final MP4, poster, captions,
  page/hash lineage, download authorization, and retention/deletion policy.
- Restore PostgreSQL and object storage into an empty isolated environment and
  reconcile Run, step, review, artifact, and object hashes.
