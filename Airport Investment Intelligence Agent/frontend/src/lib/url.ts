/**
 * Returns a normalized absolute http(s) URL, or null for anything else: relative or
 * protocol-relative URLs, javascript:/data:/mailto: schemes and URLs carrying credentials.
 * Only URLs that pass this check are ever rendered as links.
 */
export function safeHttpUrl(raw: string | null | undefined): string | null {
  if (!raw) return null;
  let url: URL;
  try {
    url = new URL(raw.trim());
  } catch {
    return null;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return null;
  if (url.username || url.password) return null;
  return url.href;
}
