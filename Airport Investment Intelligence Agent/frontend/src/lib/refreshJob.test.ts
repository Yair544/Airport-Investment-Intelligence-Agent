import { describe, expect, it, vi } from "vitest";
import { NetworkError } from "../api/client";
import { fakeClock } from "../testing/fakeClock";
import type { RefreshJob } from "../types";
import { followRefreshJob, refreshSummary } from "./refreshJob";

function job(extra: Partial<RefreshJob> = {}): RefreshJob {
  return {
    job_id: "job-1",
    status: "queued",
    stage: "queued",
    created_at: "2026-10-23T10:00:00Z",
    started_at: null,
    finished_at: null,
    sources: [],
    changed: null,
    active_version_id: null,
    error: null,
    status_url: "/api/data/refresh/job-1",
    ...extra,
  };
}

describe("refreshSummary", () => {
  it("describes each outcome as the spec requires", () => {
    expect(refreshSummary(job({ status: "running", stage: "downloading_files" })).text).toBe("Refreshing: downloading files");
    expect(refreshSummary(job({ status: "completed", changed: true }))).toEqual({ tone: "success", text: "Data updated" });
    expect(refreshSummary(job({ status: "completed", changed: false }))).toEqual({ tone: "neutral", text: "No new data found" });
    expect(refreshSummary(job({ status: "failed" }))).toEqual({
      tone: "error",
      text: "Refresh failed — previous data version remains active",
    });
    expect(refreshSummary(job({ status: "interrupted" })).tone).toBe("error");
  });
});

describe("followRefreshJob", () => {
  it("polls every two seconds until the job finishes", async () => {
    const clock = fakeClock();
    const getRefreshJob = vi
      .fn<(id: string) => Promise<RefreshJob>>()
      .mockResolvedValueOnce(job({ status: "running", stage: "downloading" }))
      .mockResolvedValueOnce(job({ status: "completed", changed: false }));
    const updates: RefreshJob[] = [];

    const outcome = await followRefreshJob(job(), {
      api: { getRefreshJob },
      signal: new AbortController().signal,
      clock,
      onUpdate: (update) => updates.push(update),
    });

    expect(outcome).toMatchObject({ kind: "finished", job: { status: "completed", changed: false } });
    expect(clock.sleeps).toEqual([2000, 2000]);
    expect(updates.map((update) => update.status)).toEqual(["running", "completed"]);
  });

  it("stops after repeated connection errors", async () => {
    const getRefreshJob = vi.fn<(id: string) => Promise<RefreshJob>>().mockRejectedValue(new NetworkError("offline", false));
    const outcome = await followRefreshJob(job({ status: "running" }), {
      api: { getRefreshJob },
      signal: new AbortController().signal,
      clock: fakeClock(),
      onUpdate: () => {},
    });
    expect(outcome.kind).toBe("lost");
    expect(getRefreshJob).toHaveBeenCalledTimes(5);
  });
});
