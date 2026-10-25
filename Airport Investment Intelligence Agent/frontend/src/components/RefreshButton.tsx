import { useEffect, useId, useRef, useState } from "react";
import { errorMessage, isCancellation, type SessionApi } from "../api/client";
import { formatDateTime, NO_DATA } from "../lib/format";
import { humanize } from "../lib/format";
import { sourceLabel } from "../lib/labels";
import { followRefreshJob, isRefreshActive, refreshSummary, stageLabel } from "../lib/refreshJob";
import type { RefreshJob, SourceRefreshResult } from "../types";

const SOURCE_STATUS_LABELS: Record<SourceRefreshResult["status"], string> = {
  pending: "Pending",
  unchanged: "Unchanged",
  updated: "Updated",
  failed: "Failed",
  skipped: "Skipped",
};

function JobDetails({ job }: { job: RefreshJob }) {
  return (
    <>
      <dl className="facts facts--inline">
        <div>
          <dt>Status</dt>
          <dd>
            {humanize(job.status)}
            {job.stage && job.stage !== job.status ? ` · ${stageLabel(job.stage)}` : ""}
          </dd>
        </div>
        <div>
          <dt>Started</dt>
          <dd>{job.started_at ? formatDateTime(job.started_at) : "Not yet"}</dd>
        </div>
        <div>
          <dt>Finished</dt>
          <dd>{job.finished_at ? formatDateTime(job.finished_at) : "Not yet"}</dd>
        </div>
        {job.active_version_id && (
          <div>
            <dt>Active data version</dt>
            <dd>
              <code>{job.active_version_id}</code>
            </dd>
          </div>
        )}
      </dl>
      {job.error && <p className="alert alert--error">{job.error.message}</p>}
      {job.sources.length === 0 ? (
        <p className="muted">No per-source results yet.</p>
      ) : (
        <div className="table-wrap">
          <table className="data data--compact">
            <thead>
              <tr>
                <th scope="col">Source</th>
                <th scope="col">Result</th>
                <th scope="col">Periods</th>
                <th scope="col">Checked</th>
                <th scope="col">Retrieved</th>
              </tr>
            </thead>
            <tbody>
              {job.sources.map((source) => (
                <tr key={source.source_id}>
                  <th scope="row">{sourceLabel(source.source_id)}</th>
                  <td>
                    {SOURCE_STATUS_LABELS[source.status] ?? humanize(source.status)}
                    {source.message && <span className="cell-sub">{source.message}</span>}
                  </td>
                  <td>{source.periods.length ? source.periods.join(", ") : NO_DATA}</td>
                  <td>{source.checked_at ? formatDateTime(source.checked_at) : NO_DATA}</td>
                  <td>{source.retrieved_at ? formatDateTime(source.retrieved_at) : NO_DATA}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

/** "Check & update data": starts (or joins) the single refresh job and follows it to the end. */
export function RefreshButton({ api, signal }: { api: SessionApi; signal: AbortSignal }) {
  const [job, setJob] = useState<RefreshJob | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return undefined;
    const onPointer = (event: PointerEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const start = async () => {
    if (busy) return;
    setBusy(true);
    setProblem(null);
    try {
      const first = await api.startRefresh(); // returns the active job when one is already running
      if (signal.aborted) return;
      setJob(first);
      if (isRefreshActive(first)) {
        const outcome = await followRefreshJob(first, {
          api,
          signal,
          onUpdate: (update) => {
            if (!signal.aborted) setJob(update);
          },
        });
        if (signal.aborted) return;
        setJob(outcome.job);
        if (outcome.kind === "lost") setProblem(outcome.message);
      }
    } catch (error) {
      if (isCancellation(error) || signal.aborted) return;
      setProblem(errorMessage(error));
    } finally {
      if (!signal.aborted) setBusy(false);
    }
  };

  const summary = job ? refreshSummary(job) : null;
  let statusText: string | null = summary?.text ?? null;
  let tone = summary?.tone ?? "neutral";
  if (problem) {
    statusText = job ? `Refresh status unknown: ${problem}` : `Refresh could not start: ${problem}`;
    tone = "error";
  } else if (job && tone === "error" && job.error?.message) {
    statusText = `${statusText}: ${job.error.message}`;
  }

  return (
    <div className="refresh" ref={rootRef}>
      <button type="button" className="btn" onClick={() => void start()} disabled={busy}>
        {busy && <span className="spinner spinner--small" aria-hidden="true" />}
        {busy ? "Updating data…" : "Check & update data"}
      </button>
      <div className="refresh__info">
        <span className={`refresh__status refresh__status--${tone}`} role="status">
          {statusText}
        </span>
        {job && (
          <button
            type="button"
            className="btn btn--ghost btn--small"
            aria-expanded={open}
            aria-controls={panelId}
            onClick={() => setOpen((value) => !value)}
          >
            Details
          </button>
        )}
      </div>
      {job && open && (
        <div id={panelId} className="refresh__panel" role="region" aria-label="Data refresh details">
          <JobDetails job={job} />
        </div>
      )}
    </div>
  );
}
