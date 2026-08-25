import type { FullAiRun, FullAiRunCreateRequest } from "./api/contracts";

export interface FullAiSubmissionAttempt {
  idempotencyKey: string;
  request: FullAiRunCreateRequest;
  fullAiRunId?: string;
}

interface BrowserStorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export const fullAiPendingAttemptKey = "vistora.full-ai.pending-attempt.v1";

const terminalRunStatuses = new Set(["succeeded", "failed", "cancelled"]);

export function isFullAiRunTerminal(status: string): boolean {
  return terminalRunStatuses.has(status);
}

export function shouldRetainFullAiAttempt(run: FullAiRun): boolean {
  return run.status === "reconciliation_required"
    || run.billing.requiresReconciliation
    || !isFullAiRunTerminal(run.status);
}

export function readFullAiSubmissionAttempt(
  storage: BrowserStorageLike,
): FullAiSubmissionAttempt | null {
  try {
    const raw = storage.getItem(fullAiPendingAttemptKey);
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<FullAiSubmissionAttempt>;
    const request = value.request as Partial<FullAiRunCreateRequest> | undefined;
    if (
      typeof value.idempotencyKey !== "string"
      || !value.idempotencyKey
      || (value.fullAiRunId !== undefined && (typeof value.fullAiRunId !== "string" || !value.fullAiRunId))
      || !request
      || typeof request.brief !== "string"
      || typeof request.direction !== "string"
      || typeof request.aspectRatio !== "string"
      || typeof request.durationSeconds !== "number"
      || typeof request.variantsPerScene !== "number"
      || typeof request.continuity !== "boolean"
      || typeof request.aiDisclosure !== "boolean"
      || typeof request.estimateFingerprint !== "string"
      || typeof request.maxCostMinor !== "number"
      || typeof request.currency !== "string"
    ) return null;
    return value as FullAiSubmissionAttempt;
  } catch {
    return null;
  }
}

export function saveFullAiSubmissionAttempt(
  storage: BrowserStorageLike,
  attempt: FullAiSubmissionAttempt | null,
): void {
  if (attempt) storage.setItem(fullAiPendingAttemptKey, JSON.stringify(attempt));
  else storage.removeItem(fullAiPendingAttemptKey);
}
