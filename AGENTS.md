# Vistora repository instructions

## Mandatory end-of-task audit

Before sending the final response for every task in this repository, audit the
actual worktree as it exists at that point. Perform the audit after all requested
edits so that it evaluates the result that will be handed back to the user. If
the task itself is an audit, use that audit as the mandatory closing audit rather
than repeating it.

The audit is read-only unless the user explicitly asked for fixes. Preserve all
pre-existing and concurrent user changes, including untracked files. Never clean,
reset, revert, or overwrite unrelated work in order to make a check pass.
If source files change while validation is running, treat the affected result as
stale. Re-run it on a stable snapshot, or explicitly report that concurrent writes
prevented a reproducible closing result; never substitute an earlier green result.

Cover, at minimum:

- user-visible feature feasibility, end-to-end wiring, completeness, and honest
  failure/loading/empty/recovery states;
- architecture boundaries and consistency among Web, API, Worker, database,
  queues, object storage, providers, seeds, and public contracts;
- authentication, authorization, tenant isolation, input validation, secrets,
  privacy, licensing/provenance, injection and SSRF risks, and secure defaults;
- data integrity, transactions, concurrency, idempotency, retries, cancellation,
  crash recovery, migrations, backup/restore, and irreversible operations;
- performance, capacity, external-provider cost controls, timeouts, rate limits,
  resource cleanup, and scalability bottlenecks;
- build reproducibility, dependency/supply-chain risk, test quality and coverage,
  CI/release gates, deployment readiness, observability, alerting, runbooks, and
  documentation-to-implementation drift.

Use repository-provided validation commands and exercise the most important
realistic paths that the environment permits. Do not silently replace unavailable
infrastructure or providers with mocks and call the result end-to-end. Report each
check as passed, failed, skipped, or blocked; state exactly what remains unverified.

In the final response, lead with the release/readiness judgment. List actionable
findings by severity (`P0` release blocker, `P1` high, `P2` medium, `P3` low), with
the trigger, impact, and clickable file/line evidence. Distinguish confirmed facts
from inferences. Include the commands/checks run and their outcomes. If no issue is
found in an area, say what evidence was reviewed rather than claiming blanket
safety. Never describe the project as production-ready while any required check is
failing, skipped, or blocked without an explicit residual-risk statement.
