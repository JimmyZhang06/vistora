import type { WebpageVideoRunCreateRequest } from "./api/contracts.ts";

export const webpageVideoPendingAttemptKey = "vistora.webpage-video.pending-create.v1";

export interface WebpageVideoSubmissionAttempt {
  idempotencyKey: string;
  request: WebpageVideoRunCreateRequest;
}

interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

function isCreateRequest(value: unknown): value is WebpageVideoRunCreateRequest {
  if (!value || typeof value !== "object") return false;
  const request = value as Partial<WebpageVideoRunCreateRequest>;
  return typeof request.targetUrl === "string"
    && typeof request.topic === "string"
    && ["16:9", "9:16", "1:1", "4:3"].includes(String(request.aspectRatio))
    && typeof request.durationSeconds === "number"
    && typeof request.subtitlesEnabled === "boolean"
    && Boolean(request.crawl)
    && Number.isInteger(request.crawl?.maxPages)
    && request.crawl!.maxPages >= 1
    && request.crawl!.maxPages <= 12
    && Number.isInteger(request.crawl?.maxDepth)
    && request.crawl!.maxDepth >= 0
    && request.crawl!.maxDepth <= 2
    && request.crawl!.sameOriginOnly === true
    && typeof request.crawl!.includeSitemap === "boolean"
    && request.publicPageConfirmed === true
    && request.rightsConfirmed === true;
}

export function readWebpageVideoSubmissionAttempt(storage: StorageLike): WebpageVideoSubmissionAttempt | null {
  const raw = storage.getItem(webpageVideoPendingAttemptKey);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as Partial<WebpageVideoSubmissionAttempt>;
    return typeof parsed.idempotencyKey === "string"
      && parsed.idempotencyKey.startsWith("webpage-video-create:")
      && isCreateRequest(parsed.request)
      ? { idempotencyKey: parsed.idempotencyKey, request: parsed.request }
      : null;
  } catch {
    return null;
  }
}

export function saveWebpageVideoSubmissionAttempt(
  storage: StorageLike,
  attempt: WebpageVideoSubmissionAttempt | null,
) {
  if (attempt) storage.setItem(webpageVideoPendingAttemptKey, JSON.stringify(attempt));
  else storage.removeItem(webpageVideoPendingAttemptKey);
}
