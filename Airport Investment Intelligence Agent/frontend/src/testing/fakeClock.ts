import type { Clock } from "../lib/polling";

/** A clock whose sleeps resolve immediately and advance its time, recording every wait. */
export function fakeClock(start = 0): Clock & { sleeps: number[] } {
  let now = start;
  const sleeps: number[] = [];
  return {
    now: () => now,
    sleep: async (ms: number, signal?: AbortSignal) => {
      if (signal?.aborted) throw signal.reason;
      sleeps.push(ms);
      now += ms;
    },
    sleeps,
  };
}
