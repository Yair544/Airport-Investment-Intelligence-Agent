import { ApiError, NetworkError, type SessionApi } from "../api/client";
import type { RefreshJob } from "../types";
import { realClock, type Clock } from "./polling";

export const REFRESH_POLL_INTERVAL_MS = 2000;
const MAX_CONSECUTIVE_ERRORS = 5;
// Safety net above the server's refresh time budget (3 h); the server also reports expired jobs.
export const REFRESH_MAX_FOLLOW_MS = 4 * 60 * 60 * 1000;

export function isRefreshActive(job: RefreshJob): boolean {
  return job.status === "queued" || job.status === "running";
}

export type RefreshTone = "progress" | "success" | "neutral" | "error";

/** One-line status for the header, following spec section 7. */
export function refreshSummary(job: RefreshJob): { tone: RefreshTone; text: string } {
  switch (job.status) {
    case "queued":
      return { tone: "progress", text: "Refresh queued" };
    case "running":
      return { tone: "progress", text: job.stage ? `Refreshing: ${stageLabel(job.stage)}` : "Refreshing" };
    case "completed":
      return job.changed
        ? { tone: "success", text: "Data updated" }
        : { tone: "neutral", text: "No new data found" };
    default:
      return { tone: "error", text: "Refresh failed — previous data version remains active" };
  }
}

export function stageLabel(stage: string): string {
  const text = stage.replace(/[_-]+/g, " ").trim();
  return text ? text.charAt(0).toLowerCase() + text.slice(1) : stage;
}

export type RefreshFollowOutcome =
  | { kind: "finished"; job: RefreshJob }
  | { kind: "lost"; job: RefreshJob; message: string };

/** Polls a refresh job every two seconds while it is queued or running. */
export async function followRefreshJob(
  job: RefreshJob,
  deps: {
    api: Pick<SessionApi, "getRefreshJob">;
    signal: AbortSignal;
    onUpdate: (job: RefreshJob) => void;
    clock?: Clock;
    intervalMs?: number;
    maxFollowMs?: number;
  },
): Promise<RefreshFollowOutcome> {
  const clock = deps.clock ?? realClock;
  let current = job;
  let errors = 0;
  const started = clock.now();
  const maxFollowMs = deps.maxFollowMs ?? REFRESH_MAX_FOLLOW_MS;
  while (isRefreshActive(current)) {
    if (clock.now() - started > maxFollowMs) {
      return { kind: "lost", job: current, message: "Stopped checking the refresh after a long wait. Reload to see its status." };
    }
    await clock.sleep(deps.intervalMs ?? REFRESH_POLL_INTERVAL_MS, deps.signal);
    try {
      current = await deps.api.getRefreshJob(current.job_id);
      errors = 0;
      deps.onUpdate(current);
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) {
        return { kind: "lost", job: current, message: "The refresh job could not be found." };
      }
      if (!(error instanceof ApiError || error instanceof NetworkError)) throw error;
      errors += 1;
      if (errors >= MAX_CONSECUTIVE_ERRORS) {
        return { kind: "lost", job: current, message: "Lost contact with the server while checking the refresh." };
      }
    }
  }
  return { kind: "finished", job: current };
}
