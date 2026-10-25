// Lifecycle of one chat request (spec sections 6 and 7): send, follow a 202 or a lost connection
// by polling the request status, wait while another request in the conversation is processing,
// and classify failures by how they can be retried. React-free so it can be tested directly.

import { ApiError, NetworkError, type SessionApi } from "../api/client";
import type { ChatRequestStatus, ChatResponse, ConversationDetail } from "../types";
import { backoffDelay, CHAT_POLL_MAX_MS, poll, realClock, type Clock } from "./polling";

export interface OutgoingRequest {
  /** Created once per new message and reused for every retry of it. */
  requestId: string;
  /** Exactly the text first sent; the server rejects a reused id with different content. */
  message: string;
  /** The conversation_id originally sent (null for a first message); retries send it unchanged. */
  sentConversationId: string | null;
}

/** How a failed request can continue: resend the same request, send a new message, or neither. */
export type RetryMode = "same" | "new_message" | "none";

export interface RequestFailure {
  status: "failed" | "interrupted" | "error";
  code: string;
  message: string;
  retry: RetryMode;
  /** This response proves the server stored the user message (it stays in the history). */
  registered: boolean;
  conversationId: string | null;
  httpStatus: number | null;
  retryAfterSeconds: number | null;
}

export type ChatOutcome =
  | { kind: "completed"; response: ChatResponse }
  | { kind: "failed"; failure: RequestFailure };

export type ChatProgress =
  | { type: "phase"; phase: "sending" | "waiting" | "queued" }
  /** The server has the request; for a first message this reveals the new conversation id. */
  | { type: "accepted"; conversationId: string }
  /** Fresh transcript while this request waits behind another one in the same conversation. */
  | { type: "conversation"; detail: ConversationDetail };

export interface RunnerLimits {
  /** Time budget for status polling after a 202 or a lost connection. */
  pollMaxMs: number;
  /** Time budget for waiting on another request in the same conversation (409 conversation_busy). */
  busyMaxMs: number;
  /** Consecutive 404s before concluding the server never received the request. */
  notFoundLimit: number;
  /** Consecutive unreachable-server polls before giving up on a request the server may not have. */
  unreachableLimit: number;
}

export const DEFAULT_RUNNER_LIMITS: RunnerLimits = {
  pollMaxMs: CHAT_POLL_MAX_MS,
  busyMaxMs: CHAT_POLL_MAX_MS,
  notFoundLimit: 3,
  unreachableLimit: 4,
};

export interface RunnerDeps {
  api: Pick<SessionApi, "postChat" | "getChatRequest" | "getConversation">;
  /** The session signal: aborting it stops all waiting and rejects with its reason. */
  signal: AbortSignal;
  clock?: Clock;
  limits?: Partial<RunnerLimits>;
  onProgress?: (progress: ChatProgress) => void;
}

/** Failure codes that reject a request before it is stored. */
const NOT_STORED_CODES = new Set([
  "conversation_busy",
  "conversation_not_found",
  "request_id_conflict",
  "too_many_active_requests",
  "system_busy",
  "rate_limited",
]);

/** Maps an HTTP error from POST /api/chat to what the UI can offer next. */
export function failureFromError(error: ApiError): RequestFailure {
  const failure: RequestFailure = {
    status: "error",
    code: error.code,
    message: error.message,
    retry: "same",
    registered: error.requestId !== null && !NOT_STORED_CODES.has(error.code),
    conversationId: error.conversationId,
    httpStatus: error.status,
    retryAfterSeconds: error.retryAfterSeconds,
  };
  switch (error.code) {
    case "stale_request":
    case "attempt_limit_reached":
      return { ...failure, registered: true, retry: "new_message" };
    case "request_id_conflict":
      return { ...failure, retry: "new_message" };
    case "conversation_not_found":
      return { ...failure, retry: "none" };
    default:
      break;
  }
  if (error.status === 404 || error.status === 413 || error.status === 415 || error.status === 422) {
    return { ...failure, retry: "none" };
  }
  return failure; // 403, 409 busy, 429, 5xx and unknown errors: the same request can be resent
}

function failureFromStatus(status: ChatRequestStatus): RequestFailure {
  const interrupted = status.status === "interrupted";
  return {
    status: interrupted ? "interrupted" : "failed",
    code: status.error?.code ?? status.status,
    message:
      status.error?.message ||
      (interrupted
        ? "Processing stopped before an answer was saved. You can retry the same request."
        : "The request failed before an answer was saved."),
    retry: "same",
    registered: true,
    conversationId: status.conversation_id,
    httpStatus: null,
    retryAfterSeconds: null,
  };
}

function clientFailure(
  code: string,
  message: string,
  retry: RetryMode,
  registered: boolean,
  conversationId: string | null = null,
): RequestFailure {
  return { status: "error", code, message, retry, registered, conversationId, httpStatus: null, retryAfterSeconds: null };
}

/** No answer can be expected from the server right now (no response, or a gateway/server error). */
function isUnavailable(error: unknown): boolean {
  return error instanceof NetworkError || (error instanceof ApiError && error.status >= 500);
}

/** After these POST failures the request may still be processing, so its status must be checked. */
function outcomeUnknown(error: unknown): boolean {
  return (
    error instanceof NetworkError ||
    (error instanceof ApiError && !error.fromServer && (error.status === 502 || error.status === 503 || error.status === 504))
  );
}

/** Id of a request other than `ownRequestId` that the transcript shows as processing. */
export function processingRequestId(detail: ConversationDetail, ownRequestId: string): string | null {
  for (const message of detail.messages) {
    const request = message.role === "user" ? message.request : null;
    if (request && request.status === "processing" && request.request_id !== ownRequestId) return request.request_id;
  }
  return null;
}

/** Sends a chat request and follows it to a final answer or a classified failure. */
export async function sendChat(request: OutgoingRequest, deps: RunnerDeps): Promise<ChatOutcome> {
  const clock = deps.clock ?? realClock;
  const limits = { ...DEFAULT_RUNNER_LIMITS, ...deps.limits };
  const busyDeadline = clock.now() + limits.busyMaxMs;

  for (;;) {
    deps.onProgress?.({ type: "phase", phase: "sending" });
    try {
      const result = await deps.api.postChat({
        request_id: request.requestId,
        message: request.message,
        conversation_id: request.sentConversationId,
      });
      if (result.kind === "completed") return { kind: "completed", response: result.response };
      deps.onProgress?.({ type: "accepted", conversationId: result.accepted.conversation_id });
      return await followRequest(request, deps, true);
    } catch (error) {
      if (outcomeUnknown(error)) return await followRequest(request, deps, false);
      if (!(error instanceof ApiError)) throw error;
      if (error.code === "conversation_busy") {
        const conversationId = error.conversationId ?? request.sentConversationId;
        if (conversationId !== null && clock.now() < busyDeadline) {
          deps.onProgress?.({ type: "phase", phase: "queued" });
          if (await waitUntilIdle(conversationId, request.requestId, deps, busyDeadline)) continue;
        }
      }
      return { kind: "failed", failure: failureFromError(error) };
    }
  }
}

/** Follows a request the server already has, e.g. one a saved conversation shows as processing. */
export function resumeChat(request: OutgoingRequest, deps: RunnerDeps): Promise<ChatOutcome> {
  return followRequest(request, deps, true);
}

async function followRequest(
  request: OutgoingRequest,
  deps: RunnerDeps,
  knownRegistered: boolean,
): Promise<ChatOutcome> {
  const limits = { ...DEFAULT_RUNNER_LIMITS, ...deps.limits };
  deps.onProgress?.({ type: "phase", phase: "waiting" });
  let registered = knownRegistered;
  let notFound = 0;
  let unreachable = 0;

  const result = await poll<ChatOutcome>(
    async () => {
      try {
        const status = await deps.api.getChatRequest(request.requestId);
        notFound = 0;
        unreachable = 0;
        if (!registered) {
          registered = true;
          deps.onProgress?.({ type: "accepted", conversationId: status.conversation_id });
        }
        if (status.status === "processing") return { done: false };
        if (status.status === "completed") {
          if (status.response) return { done: true, value: { kind: "completed", response: status.response } };
          const failure = clientFailure(
            "missing_response",
            "The answer was saved but could not be loaded. Retry to fetch it.",
            "same",
            true,
            status.conversation_id,
          );
          return { done: true, value: { kind: "failed", failure } };
        }
        return { done: true, value: { kind: "failed", failure: failureFromStatus(status) } };
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) {
          notFound += 1;
          if (notFound < limits.notFoundLimit) return { done: false };
          const failure = registered
            ? clientFailure("request_not_found", "This request is no longer available. Reload the conversation.", "none", true)
            : clientFailure(
                "not_received",
                "The server has no record of this message, so it was probably not received. Retry to send it again.",
                "same",
                false,
              );
          return { done: true, value: { kind: "failed", failure } };
        }
        if (error instanceof ApiError && error.status === 429) {
          return { done: false, delayMs: Math.max(1000, (error.retryAfterSeconds ?? 5) * 1000) };
        }
        if (isUnavailable(error)) {
          unreachable += 1;
          if (registered || unreachable < limits.unreachableLimit) return { done: false };
          const failure = clientFailure(
            "unreachable",
            "Could not reach the server to confirm this message. Check your connection and retry.",
            "same",
            false,
          );
          return { done: true, value: { kind: "failed", failure } };
        }
        if (error instanceof ApiError) return { done: true, value: { kind: "failed", failure: failureFromError(error) } };
        throw error;
      }
    },
    { signal: deps.signal, clock: deps.clock, maxMs: limits.pollMaxMs },
  );

  if (result.done) return result.value;
  return {
    kind: "failed",
    failure: clientFailure(
      "status_timeout",
      "No answer yet. The server may still be working on it; retry to check again.",
      "same",
      registered,
    ),
  };
}

/** Polls the conversation until no other request is processing. False when the wait times out. */
async function waitUntilIdle(
  conversationId: string,
  ownRequestId: string,
  deps: RunnerDeps,
  deadline: number,
): Promise<boolean> {
  const clock = deps.clock ?? realClock;
  for (let attempt = 0; ; attempt += 1) {
    const wait = backoffDelay(attempt);
    if (clock.now() + wait > deadline) return false;
    await clock.sleep(wait, deps.signal);
    try {
      const detail = await deps.api.getConversation(conversationId);
      deps.onProgress?.({ type: "conversation", detail });
      const blocking = processingRequestId(detail, ownRequestId);
      if (blocking === null) return true;
      // A status read also turns a request whose processing lease expired into "interrupted".
      const status = await deps.api.getChatRequest(blocking);
      if (status.status !== "processing") {
        deps.onProgress?.({ type: "conversation", detail: await deps.api.getConversation(conversationId) });
        return true;
      }
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) return false;
      if (!(error instanceof ApiError || error instanceof NetworkError)) throw error;
    }
  }
}
