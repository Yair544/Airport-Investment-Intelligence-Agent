import type { ReactNode } from "react";
import { safeHttpUrl } from "../lib/url";

/** Opens absolute http(s) URLs in a new tab; anything else is rendered as plain text. */
export function ExternalLink({ href, children }: { href: string | null | undefined; children: ReactNode }) {
  const url = safeHttpUrl(href);
  if (!url) return <span>{children}</span>;
  return (
    <a href={url} target="_blank" rel="noopener noreferrer nofollow">
      {children}
    </a>
  );
}
