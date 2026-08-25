const loopbackHosts = new Set(["127.0.0.1", "localhost", "::1", "[::1]"]);

/**
 * Keep production media HTTPS-only while allowing the explicit loopback HTTP
 * endpoint used by the local MinIO development profile.
 */
export function safeBrowserMediaUrl(
  value?: string,
  currentPageUrl?: string,
): string | undefined {
  if (!value) return undefined;
  try {
    const parsed = new URL(value);
    if (parsed.protocol === "https:") return parsed.href;
    if (parsed.protocol !== "http:" || !loopbackHosts.has(parsed.hostname)) return undefined;

    const pageValue = currentPageUrl
      ?? (typeof window === "undefined" ? undefined : window.location.href);
    if (!pageValue) return undefined;
    const page = new URL(pageValue);
    return page.protocol === "http:" && loopbackHosts.has(page.hostname)
      ? parsed.href
      : undefined;
  } catch {
    return undefined;
  }
}
