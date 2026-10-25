import { afterEach, describe, expect, it, vi } from "vitest";
import { sampleResponses } from "../dev/mockData";
import {
  ApiError,
  createApiClient,
  NetworkError,
  parseRetryAfter,
  SessionEndedError,
  type FetchLike,
  type SessionOptions,
} from "./client";

function jsonResponse(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
}

function sessionOptions(overrides: Partial<SessionOptions> = {}): SessionOptions {
  return {
    signal: new AbortController().signal,
    csrfToken: () => "csrf-123",
    onUnauthorized: vi.fn(),
    ...overrides,
  };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("parseRetryAfter", () => {
  it("reads delta-seconds and HTTP dates", () => {
    expect(parseRetryAfter("30")).toBe(30);
    const now = Date.parse("2026-10-23T12:00:00Z");
    expect(parseRetryAfter("Fri, 23 Oct 2026 12:00:45 GMT", now)).toBe(45);
    expect(parseRetryAfter("Fri, 23 Oct 2026 11:00:00 GMT", now)).toBe(0);
    expect(parseRetryAfter("soon")).toBeNull();
    expect(parseRetryAfter(null)).toBeNull();
  });
});

describe("session API", () => {
  it("sends the CSRF token only on state-changing requests and keeps cookies same-origin", async () => {
    const calls: Array<{ input: string; init: RequestInit }> = [];
    const fetchImpl: FetchLike = async (input, init) => {
      calls.push({ input, init });
      return input === "/api/chat"
        ? jsonResponse(200, sampleResponses.clarification)
        : jsonResponse(200, { conversations: [] });
    };
    const api = createApiClient(fetchImpl).session(sessionOptions());

    await api.listConversations();
    const result = await api.postChat({ request_id: "r1", message: "Hello", conversation_id: null });

    expect(result).toEqual({ kind: "completed", response: sampleResponses.clarification });
    const [get, post] = calls;
    expect(new Headers(get?.init.headers).get("X-CSRF-Token")).toBeNull();
    expect(new Headers(post?.init.headers).get("X-CSRF-Token")).toBe("csrf-123");
    expect(new Headers(post?.init.headers).get("Content-Type")).toBe("application/json");
    expect(post?.init.credentials).toBe("same-origin");
    expect(JSON.parse(String(post?.init.body))).toEqual({ request_id: "r1", message: "Hello", conversation_id: null });
  });

  it("distinguishes 202 Accepted from a final answer", async () => {
    const accepted = { request_id: "r1", conversation_id: "c1", status: "processing", status_url: "/api/chat/requests/r1" };
    const api = createApiClient(async () => jsonResponse(202, accepted)).session(sessionOptions());
    await expect(api.postChat({ request_id: "r1", message: "Hi", conversation_id: null })).resolves.toEqual({
      kind: "accepted",
      accepted,
    });
  });

  it("parses the error envelope with ids and Retry-After", async () => {
    const api = createApiClient(async () =>
      jsonResponse(
        429,
        { error: { code: "rate_limited", message: "Slow down.", request_id: "r1", conversation_id: "c1" } },
        { "Retry-After": "12" },
      ),
    ).session(sessionOptions());

    const error = await api.getChatRequest("r1").catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 429,
      code: "rate_limited",
      message: "Slow down.",
      requestId: "r1",
      conversationId: "c1",
      retryAfterSeconds: 12,
      fromServer: true,
    });
  });

  it("marks errors that are not the backend envelope", async () => {
    const api = createApiClient(async () => new Response("<html>Bad gateway</html>", { status: 502 })).session(
      sessionOptions(),
    );
    const error = await api.listConversations().catch((caught: unknown) => caught);
    expect(error).toMatchObject({ status: 502, code: "http_502", fromServer: false });
  });

  it("ends the session on 401", async () => {
    const onUnauthorized = vi.fn();
    const api = createApiClient(async () =>
      jsonResponse(401, { error: { code: "not_authenticated", message: "Sign in." } }),
    ).session(sessionOptions({ onUnauthorized }));
    await expect(api.listConversations()).rejects.toBeInstanceOf(SessionEndedError);
    expect(onUnauthorized).toHaveBeenCalledOnce();
  });

  it("cancels calls once the session signal aborts", async () => {
    const controller = new AbortController();
    const fetchImpl: FetchLike = (_input, init) =>
      new Promise((_resolve, reject) => init.signal?.addEventListener("abort", () => reject(init.signal?.reason)));
    const api = createApiClient(fetchImpl).session(sessionOptions({ signal: controller.signal }));
    const pending = api.getConversation("c1");
    controller.abort(new SessionEndedError());
    await expect(pending).rejects.toBeInstanceOf(SessionEndedError);
    await expect(api.getConversation("c1")).rejects.toBeInstanceOf(SessionEndedError);
  });

  it("reports a client-side timeout as a network error", async () => {
    vi.useFakeTimers();
    const fetchImpl: FetchLike = (_input, init) =>
      new Promise((_resolve, reject) => init.signal?.addEventListener("abort", () => reject(init.signal?.reason)));
    const api = createApiClient(fetchImpl).session(sessionOptions());
    const pending = api.getChatRequest("r1").catch((caught: unknown) => caught);
    await vi.advanceTimersByTimeAsync(30_000);
    const error = await pending;
    expect(error).toBeInstanceOf(NetworkError);
    expect(error).toMatchObject({ timedOut: true });
  });

  it("reports a failed connection as a network error", async () => {
    const api = createApiClient(async () => {
      throw new TypeError("Failed to fetch");
    }).session(sessionOptions());
    const error = await api.listConversations().catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(NetworkError);
    expect(error).toMatchObject({ timedOut: false });
  });
});

describe("auth calls", () => {
  it("returns null from me() when signed out", async () => {
    const client = createApiClient(async () => jsonResponse(401, { error: { code: "not_authenticated", message: "No." } }));
    await expect(client.me()).resolves.toBeNull();
  });

  it("posts credentials as JSON and returns the user and CSRF token", async () => {
    const auth = { user: { user_id: "u1", username: "analyst" }, csrf_token: "t1" };
    let body: unknown;
    const client = createApiClient(async (_input, init) => {
      body = JSON.parse(String(init.body));
      return jsonResponse(200, auth);
    });
    await expect(client.login("analyst", "secret")).resolves.toEqual(auth);
    expect(body).toEqual({ username: "analyst", password: "secret" });
  });

  it("posts a new account to the register endpoint", async () => {
    const auth = { user: { user_id: "u2", username: "new.analyst" }, csrf_token: "t2" };
    let url = "";
    const client = createApiClient(async (input) => {
      url = String(input);
      return jsonResponse(200, auth);
    });
    await expect(client.register("new.analyst", "a-long-password")).resolves.toEqual(auth);
    expect(url).toBe("/api/auth/register");
  });

  it("sends the CSRF token on logout and accepts 204", async () => {
    let token: string | null = null;
    const client = createApiClient(async (_input, init) => {
      token = new Headers(init.headers).get("X-CSRF-Token");
      return new Response(null, { status: 204 });
    });
    await expect(client.logout("csrf-9")).resolves.toBeUndefined();
    expect(token).toBe("csrf-9");
  });
});
