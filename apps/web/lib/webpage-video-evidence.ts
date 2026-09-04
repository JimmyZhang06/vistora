import type { WebpageVideoRun, WebpageVideoSitePlan } from "@/lib/api";

export interface WebpageVideoEvidenceReport {
  schemaVersion: "1.1.0";
  reportType: "vistora.webpage-video.application-evidence";
  generatedAt: string;
  run: Record<string, unknown>;
  sourceEvidence: Record<string, unknown>;
  humanGates: Record<string, unknown>;
  outputEvidence: Record<string, unknown>;
  evidenceChain: WebpageVideoShotEvidence[];
  pilotOutcome: Record<string, unknown> | null;
  completeness: { complete: boolean; missing: string[] };
  limitations: string[];
}

export interface WebpageVideoShotEvidence {
  shotId: string;
  order: number;
  label: string;
  pageId: string;
  regionId: string | null;
  sourceUrl: string | null;
  pageTitle: string | null;
  pageCaptureSha256: string | null;
  regionSha256: string | null;
  scopeSha256: string | null;
  storyboardSha256: string | null;
  outputSha256: string | null;
  scopeStatus: string | null;
  storyboardStatus: string | null;
  complete: boolean;
  missing: string[];
}

export function buildWebpageVideoShotEvidence(
  run: WebpageVideoRun,
  site?: WebpageVideoSitePlan,
): WebpageVideoShotEvidence[] {
  if (!site?.storyboard) return [];
  const pages = new Map(site.scope.pages.map((page) => [page.id, page]));
  return site.storyboard.shots
    .filter((shot) => shot.enabled)
    .sort((left, right) => left.order - right.order)
    .map((shot) => {
      const page = pages.get(shot.pageId);
      const region = page?.regions.find((item) => item.id === shot.regionId);
      const pageCaptureSha256 = page?.capture?.sha256 ?? null;
      const regionSha256 = region?.sha256 ?? null;
      const missing: string[] = [];
      if (!page?.url) missing.push("source_url");
      if (!pageCaptureSha256 && !regionSha256) missing.push("visual_source_hash");
      if (!site.scope.sha256) missing.push("scope_hash");
      if (!site.storyboard?.sha256) missing.push("storyboard_hash");
      if (!run.finalVideo?.sha256) missing.push("final_video_hash");
      return {
        shotId: shot.id,
        order: shot.order,
        label: shot.label,
        pageId: shot.pageId,
        regionId: shot.regionId ?? null,
        sourceUrl: page?.finalUrl ?? page?.url ?? null,
        pageTitle: page?.title ?? null,
        pageCaptureSha256,
        regionSha256,
        scopeSha256: site.scope.sha256 || null,
        storyboardSha256: site.storyboard?.sha256 || null,
        outputSha256: run.finalVideo?.sha256 ?? null,
        scopeStatus: site.scope.status || null,
        storyboardStatus: site.storyboard?.status || null,
        complete: missing.length === 0,
        missing,
      };
    });
}

export function buildWebpageVideoEvidenceReport(
  run: WebpageVideoRun,
  site?: WebpageVideoSitePlan,
  generatedAt = new Date().toISOString(),
  siteEvidenceError?: string,
): WebpageVideoEvidenceReport {
  const missing: string[] = [];
  const evidenceChain = buildWebpageVideoShotEvidence(run, site);
  if (!run.capture?.sha256 && !site?.scope.sha256) missing.push("capture_hash");
  if (run.status !== "succeeded") missing.push("successful_final_run");
  if (!run.finalVideo?.sha256) missing.push("final_video_hash");
  if (!run.pilotFeedback) missing.push("pilot_outcome");
  if (run.siteMode && !site) missing.push("site_scope_and_storyboard");
  if (run.siteMode && site?.storyboard && !evidenceChain.length) missing.push("enabled_shot_evidence");
  if (evidenceChain.some((item) => !item.complete)) missing.push("complete_shot_evidence_chain");

  return {
    schemaVersion: "1.1.0",
    reportType: "vistora.webpage-video.application-evidence",
    generatedAt,
    run: {
      id: run.id,
      projectRunId: run.projectRunId ?? null,
      workspaceId: run.workspaceId ?? null,
      status: run.status,
      rawStatus: run.rawStatus,
      revision: run.revision,
      siteMode: run.siteMode,
      createdAt: run.createdAt ?? null,
      updatedAt: run.updatedAt ?? null,
      spec: run.spec,
      failure: run.failure ?? null,
    },
    sourceEvidence: {
      requestedUrl: run.capture?.requestedUrl ?? run.targetUrl,
      finalUrl: run.capture?.finalUrl ?? run.finalUrl ?? null,
      capture: run.capture ? {
        sha256: run.capture.sha256,
        revision: run.capture.revision,
        width: run.capture.width ?? null,
        height: run.capture.height ?? null,
        capturedAt: run.capture.capturedAt ?? null,
      } : null,
      site: site ? {
        schemaVersion: site.schemaVersion,
        scope: {
          status: site.scope.status,
          revision: site.scope.revision,
          sha256: site.scope.sha256,
          pages: site.scope.pages.map((page) => ({
            id: page.id,
            url: page.url,
            finalUrl: page.finalUrl ?? null,
            title: page.title ?? null,
            pageType: page.pageType ?? null,
            selected: page.selected,
            status: page.status ?? null,
            captureSha256: page.capture?.sha256 ?? null,
            regions: page.regions.map((region) => ({
              id: region.id,
              type: region.type,
              label: region.label,
              sha256: region.sha256 ?? null,
            })),
            failure: page.failure ?? null,
          })),
        },
        storyboard: site.storyboard ? {
          status: site.storyboard.status,
          revision: site.storyboard.revision,
          sha256: site.storyboard.sha256,
          shots: site.storyboard.shots.map((shot) => ({
            id: shot.id,
            pageId: shot.pageId,
            regionId: shot.regionId ?? null,
            label: shot.label,
            enabled: shot.enabled,
            order: shot.order,
            durationSeconds: shot.durationSeconds ?? null,
            motion: shot.motion,
            transition: shot.transition,
          })),
        } : null,
      } : null,
      siteEvidenceError: siteEvidenceError ?? null,
    },
    humanGates: {
      captureReview: run.capture?.review ?? null,
      scopeStatus: site?.scope.status ?? null,
      scopeRevision: site?.scope.revision ?? null,
      storyboardStatus: site?.storyboard?.status ?? null,
      storyboardRevision: site?.storyboard?.revision ?? null,
    },
    outputEvidence: run.finalVideo ? {
      filename: run.finalVideo.filename ?? null,
      mediaType: run.finalVideo.mediaType ?? null,
      byteSize: run.finalVideo.byteSize ?? null,
      sha256: run.finalVideo.sha256 ?? null,
    } : {},
    evidenceChain,
    pilotOutcome: run.pilotFeedback ? {
      customerSegment: run.pilotFeedback.customerSegment,
      baselineMinutes: run.pilotFeedback.baselineMinutes,
      assistedMinutes: run.pilotFeedback.assistedMinutes,
      savedMinutes: run.pilotFeedback.savedMinutes,
      timeReductionPercent: run.pilotFeedback.timeReductionPercent,
      revisionCount: run.pilotFeedback.revisionCount,
      outcome: run.pilotFeedback.outcome,
      satisfactionScore: run.pilotFeedback.satisfactionScore ?? null,
      willingnessToPayHkd: run.pilotFeedback.willingnessToPayHkd ?? null,
      notes: run.pilotFeedback.notes ?? null,
      evidenceRevision: run.pilotFeedback.revision,
      updatedAt: run.pilotFeedback.updatedAt,
    } : null,
    completeness: { complete: missing.length === 0, missing },
    limitations: [
      "This report references immutable hashes and metadata; it does not embed media bytes.",
      "Short-lived preview/download URLs are intentionally excluded.",
      "Pilot metrics are self-reported and are not independently verified by Vistora.",
      "The current contract links visual shots to source evidence; sentence-level narration claims are not yet mapped to individual source passages.",
    ],
  };
}

export function webpageVideoEvidenceFilename(runId: string): string {
  return `vistora-evidence-${runId.replace(/[^A-Za-z0-9_-]/g, "-")}.json`;
}
