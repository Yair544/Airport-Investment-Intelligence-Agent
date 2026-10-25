import { describe, expect, it } from "vitest";
import { fakeClock } from "../testing/fakeClock";
import { backoffDelay, CHAT_POLL_MAX_MS, poll, sleep } from "./polling";

describe("backoffDelay", () => {
  it("waits 1 s, 2 s and 3 s, then every 5 s", () => {
    expect([0, 1, 2, 3, 4, 10].map((attempt) => backoffDelay(attempt))).toEqual([1000, 2000, 3000, 5000, 5000, 5000]);
  });
});

describe("poll", () => {
  it("checks after each delay until a result is ready", async () => {
    const clock = fakeClock();
    const attempts: number[] = [];
    const result = await poll(
      async (attempt) => {
        attempts.push(attempt);
        return attempt === 3 ? { done: true, value: "answer" } : { done: false };
      },
      { clock },
    );
    expect(result).toEqual({ done: true, value: "answer" });
    expect(attempts).toEqual([0, 1, 2, 3]);
    expect(clock.sleeps).toEqual([1000, 2000, 3000, 5000]);
  });

  it("stops before the time budget is exceeded", async () => {
    const clock = fakeClock();
    let checks = 0;
    const result = await poll(
      async () => {
        checks += 1;
        return { done: false };
      },
      { clock },
    );
    expect(result).toEqual({ done: false });
    const waited = clock.sleeps.reduce((sum, ms) => sum + ms, 0);
    expect(waited).toBeLessThanOrEqual(CHAT_POLL_MAX_MS);
    expect(waited).toBeGreaterThan(CHAT_POLL_MAX_MS - 5000);
    expect(checks).toBe(clock.sleeps.length);
  });

  it("honours a delay requested by the check (for example Retry-After)", async () => {
    const clock = fakeClock();
    let calls = 0;
    await poll(
      async () => {
        calls += 1;
        return calls === 1 ? { done: false, delayMs: 30_000 } : { done: true, value: null };
      },
      { clock },
    );
    expect(clock.sleeps).toEqual([1000, 30_000]);
  });

  it("rejects with the abort reason when the signal aborts", async () => {
    const controller = new AbortController();
    const reason = new Error("session ended");
    controller.abort(reason);
    await expect(poll(async () => ({ done: false }), { clock: fakeClock(), signal: controller.signal })).rejects.toBe(
      reason,
    );
  });
});

describe("sleep", () => {
  it("rejects as soon as the signal aborts", async () => {
    const controller = new AbortController();
    const pending = sleep(60_000, controller.signal);
    const reason = new Error("stop");
    controller.abort(reason);
    await expect(pending).rejects.toBe(reason);
  });
});
