import { describe, expect, it, vi } from "vitest";
import { ApiError, NetworkError, SessionEndedError, type PostChatResult, type SessionApi } from "../api/client";
import { sampleResponses } from "../dev/mockData";
import { fakeClock } from "../testing/fakeClock";
import type { ChatRequestStatus, ConversationDetail, ErrorBody } from "../types";
import { failureFromError, sendChat, resumeChat, type ChatProgress, type OutgoingRequest, type RunnerDeps } from "./chatRequest";

const REQUEST: OutgoingRequest = { requestId: "req-1", message: "Compare LAX and SNA", sentConversationId: "conv-1" };
const ANSWER = { ...sampleResponses.comparison, conversation_id: "conv-1" };

function status(value: ChatRequestStatus["status"], extra: Partial<ChatRequestStatus> = {}): ChatRequestStatus {
  return {
    request_id: REQUEST.requestId,
    conversation_id: "conv-1",
    status: value,
    created_at: "2026-10-23T10:00:00Z",
    updated_at: "2026-10-23T10:00:05Z",
    response: value === "completed" ? ANSWER : null,
    error: null,
    ...extra,
  };
}

function apiError(statusCode: number, body: Partial<ErrorBody> & { code: string }, options = {}): ApiError {
  return new ApiError(statusCode, { message: `${body.code} message`, ...body }, options);
}

function detail(processingRequestId: string | null): ConversationDetail {
  return {
    conversation_id: "conv-1",
    title: "Congestion",
    created_at: "2026-10-23T09:00:00Z",
    updated_at: "2026-10-23T10:00:00Z",
    messages: [
      {
        seq: 1,
        role: "user",
        content: "Earlier question from another tab",
        created_at: "2026-10-23T09:59:00Z",
        request: { request_id: "other", status: processingRequestId ? "processing" : "completed", sent_conversation_id: "conv-1" },
        response: null,
      },
    ],
  };
}

function setup() {
  const api = {
    postChat: vi.fn<(body: Parameters<SessionApi["postChat"]>[0]) => Promise<PostChatResult>>(),
    getChatRequest: vi.fn<(requestId: string) => Promise<ChatRequestStatus>>(),
    getConversation: vi.fn<(conversationId: string) => Promise<ConversationDetail>>(),
  };
  const progress: ChatProgress[] = [];
  const clock = fakeClock();
  const controller = new AbortController();
  const deps: RunnerDeps = { api, signal: controller.signal, clock, onProgress: (p) => progress.push(p) };
  return { api, progress, clock, controller, deps };
}

describe("sendChat", () => {
  it("returns the final answer from a 200 response", async () => {
    const { api, deps, progress } = setup();
    api.postChat.mockResolvedValueOnce({ kind: "completed", response: ANSWER });

    await expect(sendChat(REQUEST, deps)).resolves.toEqual({ kind: "completed", response: ANSWER });
    expect(api.postChat).toHaveBeenCalledWith({ request_id: "req-1", message: "Compare LAX and SNA", conversation_id: "conv-1" });
    expect(progress).toEqual([{ type: "phase", phase: "sending" }]);
  });

  it("polls the request status after 202 with 1 s, 2 s, 3 s back-off", async () => {
    const { api, deps, progress, clock } = setup();
    api.postChat.mockResolvedValueOnce({
      kind: "accepted",
      accepted: { request_id: "req-1", conversation_id: "conv-new", status: "processing", status_url: "/x" },
    });
    api.getChatRequest
      .mockResolvedValueOnce(status("processing"))
      .mockResolvedValueOnce(status("processing"))
      .mockResolvedValueOnce(status("completed"));

    await expect(sendChat(REQUEST, deps)).resolves.toEqual({ kind: "completed", response: ANSWER });
    expect(clock.sleeps).toEqual([1000, 2000, 3000]);
    expect(progress).toContainEqual({ type: "accepted", conversationId: "conv-new" });
    expect(progress).toContainEqual({ type: "phase", phase: "waiting" });
  });

  it("checks the status after a lost connection instead of assuming failure", async () => {
    const { api, deps, progress } = setup();
    api.postChat.mockRejectedValueOnce(new NetworkError("timeout", true));
    api.getChatRequest
      .mockRejectedValueOnce(apiError(404, { code: "request_not_found" }))
      .mockResolvedValueOnce(status("processing"))
      .mockResolvedValueOnce(status("completed"));

    await expect(sendChat(REQUEST, deps)).resolves.toEqual({ kind: "completed", response: ANSWER });
    expect(progress).toContainEqual({ type: "accepted", conversationId: "conv-1" });
  });

  it("also polls after a gateway error page, but not after a plain server error", async () => {
    const gateway = setup();
    gateway.api.postChat.mockRejectedValueOnce(apiError(504, { code: "http_504" }, { fromServer: false }));
    gateway.api.getChatRequest.mockResolvedValueOnce(status("completed"));
    await expect(sendChat(REQUEST, gateway.deps)).resolves.toMatchObject({ kind: "completed" });

    const server = setup();
    server.api.postChat.mockRejectedValueOnce(apiError(500, { code: "http_500" }, { fromServer: false }));
    const outcome = await sendChat(REQUEST, server.deps);
    expect(outcome).toMatchObject({ kind: "failed", failure: { code: "http_500", retry: "same" } });
    expect(server.api.getChatRequest).not.toHaveBeenCalled();
  });

  it("concludes the message was not received after repeated 404s", async () => {
    const { api, deps } = setup();
    api.postChat.mockRejectedValueOnce(new NetworkError("offline", false));
    api.getChatRequest.mockRejectedValue(apiError(404, { code: "request_not_found" }));

    const outcome = await sendChat(REQUEST, deps);
    expect(outcome).toMatchObject({ kind: "failed", failure: { code: "not_received", retry: "same", registered: false } });
    expect(api.getChatRequest).toHaveBeenCalledTimes(3);
  });

  it("gives up when the server stays unreachable and the request was never confirmed", async () => {
    const { api, deps } = setup();
    api.postChat.mockRejectedValueOnce(new NetworkError("offline", false));
    api.getChatRequest.mockRejectedValue(new NetworkError("offline", false));

    const outcome = await sendChat(REQUEST, deps);
    expect(outcome).toMatchObject({ kind: "failed", failure: { code: "unreachable", retry: "same", registered: false } });
    expect(api.getChatRequest).toHaveBeenCalledTimes(4);
  });

  it("reports a failed or interrupted request with its saved error", async () => {
    const failed = setup();
    failed.api.postChat.mockResolvedValueOnce({
      kind: "accepted",
      accepted: { request_id: "req-1", conversation_id: "conv-1", status: "processing", status_url: "/x" },
    });
    failed.api.getChatRequest.mockResolvedValueOnce(
      status("failed", { error: { code: "provider_error", message: "The model provider failed." } }),
    );
    expect(await sendChat(REQUEST, failed.deps)).toMatchObject({
      kind: "failed",
      failure: { status: "failed", code: "provider_error", message: "The model provider failed.", retry: "same", registered: true },
    });

    const interrupted = setup();
    const resumed = await (async () => {
      interrupted.api.getChatRequest.mockResolvedValueOnce(status("interrupted"));
      return resumeChat(REQUEST, interrupted.deps);
    })();
    expect(resumed).toMatchObject({ kind: "failed", failure: { status: "interrupted", retry: "same" } });
    expect(resumed.kind === "failed" && resumed.failure.message).toContain("retry");
  });

  it("stops polling at the time budget and offers to check again", async () => {
    const { api, deps, clock } = setup();
    api.postChat.mockResolvedValueOnce({
      kind: "accepted",
      accepted: { request_id: "req-1", conversation_id: "conv-1", status: "processing", status_url: "/x" },
    });
    api.getChatRequest.mockResolvedValue(status("processing"));

    const outcome = await sendChat(REQUEST, { ...deps, limits: { pollMaxMs: 20_000 } });
    expect(outcome).toMatchObject({ kind: "failed", failure: { code: "status_timeout", retry: "same", registered: true } });
    expect(clock.sleeps.reduce((sum, ms) => sum + ms, 0)).toBeLessThanOrEqual(20_000);
  });

  it("waits for Retry-After when status checks are rate limited", async () => {
    const { api, deps, clock } = setup();
    api.postChat.mockRejectedValueOnce(new NetworkError("timeout", true));
    api.getChatRequest
      .mockRejectedValueOnce(apiError(429, { code: "rate_limited" }, { retryAfterSeconds: 20 }))
      .mockResolvedValueOnce(status("completed"));

    await sendChat(REQUEST, deps);
    expect(clock.sleeps).toEqual([1000, 20_000]);
  });

  it("waits while another request in the conversation is processing, then sends the same request", async () => {
    const { api, deps, progress } = setup();
    api.postChat
      .mockRejectedValueOnce(apiError(409, { code: "conversation_busy", conversation_id: "conv-1" }))
      .mockResolvedValueOnce({ kind: "completed", response: ANSWER });
    api.getConversation.mockResolvedValueOnce(detail("other")).mockResolvedValueOnce(detail(null));
    api.getChatRequest.mockResolvedValueOnce({ ...status("processing"), request_id: "other" });

    await expect(sendChat(REQUEST, deps)).resolves.toEqual({ kind: "completed", response: ANSWER });
    expect(api.postChat).toHaveBeenCalledTimes(2);
    expect(api.postChat.mock.calls[1]?.[0]).toEqual(api.postChat.mock.calls[0]?.[0]);
    expect(api.getChatRequest).toHaveBeenCalledWith("other");
    expect(progress).toContainEqual({ type: "phase", phase: "queued" });
    expect(progress.filter((p) => p.type === "conversation")).toHaveLength(2);
  });

  it("gives up waiting for a busy conversation after the budget", async () => {
    const { api, deps } = setup();
    api.postChat.mockRejectedValue(apiError(409, { code: "conversation_busy" }));
    api.getConversation.mockResolvedValue(detail("other"));
    api.getChatRequest.mockResolvedValue({ ...status("processing"), request_id: "other" });

    const outcome = await sendChat(REQUEST, { ...deps, limits: { busyMaxMs: 15_000 } });
    expect(outcome).toMatchObject({ kind: "failed", failure: { code: "conversation_busy", retry: "same", registered: false } });
    expect(api.postChat).toHaveBeenCalledTimes(1);
  });

  it("stops silently when the session ends", async () => {
    const { api, deps, controller } = setup();
    api.postChat.mockResolvedValueOnce({
      kind: "accepted",
      accepted: { request_id: "req-1", conversation_id: "conv-1", status: "processing", status_url: "/x" },
    });
    api.getChatRequest.mockImplementation(async () => {
      controller.abort(new SessionEndedError());
      return status("processing");
    });
    await expect(sendChat(REQUEST, deps)).rejects.toBeInstanceOf(SessionEndedError);
  });
});

describe("failureFromError", () => {
  it("classifies how each error can be retried", () => {
    expect(failureFromError(apiError(409, { code: "stale_request", request_id: "req-1" }))).toMatchObject({
      retry: "new_message",
      registered: true,
    });
    expect(failureFromError(apiError(409, { code: "request_id_conflict", request_id: "req-1" }))).toMatchObject({
      retry: "new_message",
      registered: false,
    });
    expect(failureFromError(apiError(429, { code: "attempt_limit_reached", request_id: "req-1" }))).toMatchObject({
      retry: "new_message",
    });
    expect(failureFromError(apiError(429, { code: "too_many_active_requests" }, { retryAfterSeconds: 5 }))).toMatchObject({
      retry: "same",
      registered: false,
      retryAfterSeconds: 5,
    });
    expect(failureFromError(apiError(404, { code: "conversation_not_found" }))).toMatchObject({ retry: "none" });
    expect(failureFromError(apiError(422, { code: "validation_error" }))).toMatchObject({ retry: "none" });
    expect(failureFromError(apiError(413, { code: "payload_too_large" }))).toMatchObject({ retry: "none" });
    expect(failureFromError(apiError(403, { code: "csrf_failed" }))).toMatchObject({ retry: "same", httpStatus: 403 });
    expect(
      failureFromError(apiError(504, { code: "processing_timeout", request_id: "req-1", conversation_id: "conv-9" })),
    ).toMatchObject({ retry: "same", registered: true, conversationId: "conv-9" });
  });
});
