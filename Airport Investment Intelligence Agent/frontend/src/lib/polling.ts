// Bounded polling with an injectable clock, so the schedule is testable without real timers.

/** Chat status checks wait 1 s, 2 s and 3 s, then every 5 s, for at most five minutes. */
export const CHAT_POLL_SCHEDULE_MS: readonly number[] = [1000, 2000, 3000];
export const CHAT_POLL_STEADY_MS = 5000;
export const CHAT_POLL_MAX_MS = 5 * 60 * 1000;

export function backoffDelay(
  attempt: number,
  schedule: readonly number[] = CHAT_POLL_SCHEDULE_MS,
  steadyMs: number = CHAT_POLL_STEADY_MS,
): number {
  return schedule[attempt] ?? steadyMs;
}

/** Resolves after `ms`, or rejects with the signal's reason as soon as the signal aborts. */
export function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason);
      return;
    }
    const onAbort = () => {
      clearTimeout(timer);
      reject(signal?.reason);
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

export interface Clock {
  now(): number;
  sleep(ms: number, signal?: AbortSignal): Promise<void>;
}

export const realClock: Clock = { now: () => Date.now(), sleep };

/** One poll result: stop with a value, or continue (optionally after a server-requested delay). */
export type PollStep<T> = { done: true; value: T } | { done: false; delayMs?: number };

export interface PollOptions {
  signal?: AbortSignal;
  clock?: Clock;
  /** Total time budget, including waits. */
  maxMs?: number;
  delay?: (attempt: number) => number;
}

/**
 * Waits, then calls `check`, until it returns `done` or the next wait would exceed the budget.
 * Returns `{ done: false }` when the budget runs out; rejects if the signal aborts.
 */
export async function poll<T>(
  check: (attempt: number) => Promise<PollStep<T>>,
  options: PollOptions = {},
): Promise<{ done: true; value: T } | { done: false }> {
  const clock = options.clock ?? realClock;
  const maxMs = options.maxMs ?? CHAT_POLL_MAX_MS;
  const delay = options.delay ?? ((attempt: number) => backoffDelay(attempt));
  const started = clock.now();
  let requestedDelay: number | undefined;
  for (let attempt = 0; ; attempt += 1) {
    const wait = requestedDelay ?? delay(attempt);
    if (clock.now() - started + wait > maxMs) return { done: false };
    await clock.sleep(wait, options.signal);
    const step = await check(attempt);
    if (step.done) return step;
    requestedDelay = step.delayMs;
  }
}
