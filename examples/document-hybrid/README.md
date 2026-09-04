# Document hybrid explainer pilot

> Status: bounded local renderer smoke test, reviewed 2026-09-05. It is not a production end-to-end result.

This local pilot turns an owned PDF into a Chinese explainer video. Document
screenshots are the factual evidence layer; generated or procedural motion may
only be used as a non-factual background.

The Web entry point is `/create/document-video`. The official production pipeline v2 uses the governed document-source upload,
durable Run DAG, immutable source snapshot, persisted artifacts, review gates,
composite renderer, and retention/legal-hold/purge control plane. The command below intentionally pins pipeline v1 and remains useful as a bounded local
renderer smoke test that does not require the API, queue, or object storage:

```powershell
$env:PYTHONPATH = "services/worker"
.\.venv\Scripts\python.exe -m framefactory.worker.document_hybrid.pilot `
  --input-pdf "C:\path\to\source.pdf" `
  --storyboard "examples/document-hybrid/agnes-integration-analysis.storyboard.json" `
  --pipeline "packages/seeds/official-skills/v1/pipelines/document-hybrid-production/1.json" `
  --output-dir "output/video/document-hybrid-run"
```

The output directory must be empty. The run produces the final MP4, poster,
subtitles, source hash manifest, narration script, storyboard, provider report,
timeline, quality report, and run manifest. Windows SAPI narration, Poppler,
FFmpeg, and FFprobe must be available locally. No document text is sent to a
remote provider by the pilot. The pilot accepts unencrypted, JavaScript-free PDF
files up to 200 MiB and 100 pages; the storyboard is capped at 2 MiB and 30
scenes.

The pilot is local-only: it does not prove browser upload, presigned S3 URLs,
PostgreSQL/Redis recovery, tenant permissions, either production review gate, or
versioned-object purge. Production acceptance uses
`tools/release/document_video_e2e_gate.py` with an authorized text-layer PDF in
an isolated release workspace. Scanned-only, encrypted, or JavaScript-bearing
PDFs fail closed; OCR is not configured.
