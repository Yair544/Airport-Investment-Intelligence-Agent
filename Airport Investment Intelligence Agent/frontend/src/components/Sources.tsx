import { formatDateTime, NO_DATA } from "../lib/format";
import type { SourceRef } from "../types";
import { ExternalLink } from "./ExternalLink";

/** Source entries with the data period and the retrieval time shown separately (spec section 7). */
export function SourceList({ sources }: { sources: SourceRef[] }) {
  return (
    <ul className="source-list">
      {sources.map((source, index) => (
        <li key={`${source.source_id}-${index}`} className="source">
          <span className="source__name">
            <ExternalLink href={source.url}>{source.name}</ExternalLink>
          </span>
          <dl className="facts facts--inline">
            <div>
              <dt>Data period</dt>
              <dd>{source.period ?? NO_DATA}</dd>
            </div>
            <div>
              <dt>Retrieved</dt>
              <dd>{source.retrieved_at ? formatDateTime(source.retrieved_at) : NO_DATA}</dd>
            </div>
            {source.dataset_version && (
              <div>
                <dt>Dataset version</dt>
                <dd>{source.dataset_version}</dd>
              </div>
            )}
            {source.data_version_id && (
              <div>
                <dt>Data version</dt>
                <dd>
                  <code>{source.data_version_id}</code>
                </dd>
              </div>
            )}
          </dl>
        </li>
      ))}
    </ul>
  );
}

export function Sources({ sources }: { sources: SourceRef[] }) {
  if (sources.length === 0) return null;
  return (
    <details className="sources">
      <summary>Sources ({sources.length})</summary>
      <SourceList sources={sources} />
    </details>
  );
}
