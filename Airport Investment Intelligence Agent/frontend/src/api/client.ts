// HTTP client for the FastAPI backend. Same-origin JSON with cookie sessions: the session id lives
// only in the HttpOnly cookie, and the CSRF token is held in memory and sent on state-changing calls.

import type {
  AuthResponse,
  ChatAccepted,
  ChatRequestBody,
  ChatRequestStatus,
  ChatResponse,
  ConversationDetail,
  ConversationList,
  ErrorBody,
  RefreshJob,
} from "../types";

/** Chat answers can take about two minutes; after this the client stops waiting and polls instead. */
export const CHAT_POST_TIMEOUT_MS = 150_000;
export const DEFAULT_TIMEOUT_MS = 30_000;
const LOGOUT_TIMEOUT_MS = 10_000;

/** A non-2xx response, parsed from the uniform `{ error: {...} }` envelope when present. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | null;
  readonly conversationId: string | null;
  readonly retryAfterSeconds: number | null;
  /** False when the body was not the backend's error envelope (for example a proxy error page). */
  readonly fromServer: boolean;

  constructor(
    status: number,
    body: ErrorBody,
    options: { retryAfterSeconds?: number | null; fromServer?: boolean } = {},
  ) {
    super(body.message);
    this.name = "ApiError";
    this.status = status;
    this.code = body.code;
    this.requestId = body.request_id ?? null;
    this.conversationId = body.conversation_id ?? null;
    this.retryAfterSeconds = options.retryAfterSeconds ?? null;
    this.fromServer = options.fromServer ?? true;
  }
}

/** No HTTP response arrived: offline, connection reset, or the client-side timeout. */
export class NetworkError extends Error {
  readonly timedOut: boolean;

  constructor(message: string, timedOut: boolean) {
    super(message);
    this.name = "NetworkError";
    this.timedOut = timedOut;
  }
}

/** The session ended (sign-out, expiry or account switch); pending work must stop silently. */
export class SessionEndedError extends Error {
  constructor() {
    super("The session has ended.");
    this.name = "SessionEndedError";
  }
}

/** True for errors that mean "stop and show nothing": the session ended or the work was cancelled. */
export function isCancellation(error: unknown): boolean {
  return (
    error instanceof SessionEndedError ||
    (typeof DOMException !== "undefined" && error instanceof DOMException && error.name === "AbortError")
  );
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError || error instanceof NetworkError) return error.message;
  return "Something went wrong. Please try again.";
}

/** Retry-After as delta-seconds or an HTTP date; null when absent or unparseable. */
export function parseRetryAfter(value: string | null, nowMs: number = Date.now()): number | null {
  if (!value) return null;
  const trimmed = value.trim();
  if (/^\d+$/.test(trimmed)) return Number(trimmed);
  const date = Date.parse(trimmed);
  if (Number.isNaN(date)) return null;
  return Math.max(0, Math.ceil((date - nowMs) / 1000));
}

function defaultErrorMessage(status: number): string {
  switch (status) {
    case 401:
      return "Your session has expired. Please sign in again.";
    case 403:
      return "The request was blocked by a security check. Reload the page and try again.";
    case 404:
      return "The requested item was not found.";
    case 409:
      return "The request conflicts with the current state of the conversation.";
    case 413:
      return "The message is too large.";
    case 415:
      return "The request format is not supported.";
    case 422:
      return "The request was not valid.";
    case 429:
      return "Too many requests. Please wait a moment and try again.";
    case 502:
    case 503:
      return "The service is temporarily unavailable. Please try again shortly.";
    case 504:
      return "The server took too long to respond.";
    default:
      return "Unexpected server error. Please try again.";
  }
}

function isErrorEnvelope(value: unknown): value is { error: ErrorBody } {
  if (typeof value !== "object" || value === null) return false;
  const error = (value as { error?: unknown }).error;
  return (
    typeof error === "object" &&
    error !== null &&
    typeof (error as ErrorBody).code === "string" &&
    typeof (error as ErrorBody).message === "string"
  );
}

async function errorFromResponse(response: Response): Promise<ApiError> {
  const retryAfterSeconds = parseRetryAfter(response.headers.get("Retry-After"));
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (isErrorEnvelope(body)) {
    return new ApiError(response.status, body.error, { retryAfterSeconds, fromServer: true });
  }
  return new ApiError(
    response.status,
    { code: `http_${response.status}`, message: defaultErrorMessage(response.status) },
    { retryAfterSeconds, fromServer: false },
  );
}

export type FetchLike = (input: string, init: RequestInit) => Promise<Response>;

interface CallOptions {
  method?: "GET" | "POST";
  body?: unknown;
  csrfToken?: string;
  /** Aborting this signal (session end) rejects with SessionEndedError. */
  signal?: AbortSignal;
  timeoutMs?: number;
}

interface CallResult<T> {
  status: number;
  data: T | null;
}

async function call<T>(fetchImpl: FetchLike, path: string, options: CallOptions = {}): Promise<CallResult<T>> {
  const parent = options.signal;
  if (parent?.aborted) throw new SessionEndedError();

  const controller = new AbortController();
  let timedOut = false;
  const onParentAbort = () => controller.abort(parent?.reason);
  parent?.addEventListener("abort", onParentAbort, { once: true });
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort(new DOMException("The request timed out.", "TimeoutError"));
  }, options.timeoutMs ?? DEFAULT_TIMEOUT_MS);

  const fail = (error: unknown): never => {
    if (parent?.aborted) throw new SessionEndedError();
    if (error instanceof ApiError) throw error;
    if (timedOut) throw new NetworkError("The server did not respond in time.", true);
    throw new NetworkError("Could not reach the server. Check your connection and try again.", false);
  };

  try {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    if (options.csrfToken) headers["X-CSRF-Token"] = options.csrfToken;

    let response: Response;
    try {
      response = await fetchImpl(path, {
        method: options.method ?? "GET",
        headers,
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
        credentials: "same-origin",
        cache: "no-store",
        signal: controller.signal,
      });
    } catch (error) {
      return fail(error);
    }

    try {
      if (!response.ok) throw await errorFromResponse(response);
      if (response.status === 204) return { status: 204, data: null };
      const text = await response.text();
      if (!text) return { status: response.status, data: null };
      try {
        return { status: response.status, data: JSON.parse(text) as T };
      } catch {
        throw new ApiError(
          response.status,
          { code: "invalid_response", message: "The server returned an unreadable response." },
          { fromServer: false },
        );
      }
    } catch (error) {
      return fail(error);
    }
  } finally {
    clearTimeout(timer);
    parent?.removeEventListener("abort", onParentAbort);
  }
}

function required<T>(result: CallResult<T>): T {
  if (result.data === null) {
    throw new ApiError(
      result.status,
      { code: "invalid_response", message: "The server returned an empty response." },
      { fromServer: false },
    );
  }
  return result.data;
}

export type PostChatResult =
  | { kind: "completed"; response: ChatResponse }
  | { kind: "accepted"; accepted: ChatAccepted };

/** Calls that need a signed-in session. A 401 ends the session and rejects with SessionEndedError. */
export interface SessionApi {
  postChat(body: ChatRequestBody): Promise<PostChatResult>;
  getChatRequest(requestId: string): Promise<ChatRequestStatus>;
  listConversations(): Promise<ConversationList>;
  getConversation(conversationId: string): Promise<ConversationDetail>;
  startRefresh(): Promise<RefreshJob>;
  getRefreshJob(jobId: string): Promise<RefreshJob>;
}

export interface SessionOptions {
  /** Aborted when the session ends; every call made through this session is cancelled with it. */
  signal: AbortSignal;
  /** Read on every state-changing call so a refreshed token is picked up. */
  csrfToken: () => string;
  onUnauthorized: () => void;
}

export interface ApiClient {
  login(username: string, password: string, signal?: AbortSignal): Promise<AuthResponse>;
  /** Creates an account and signs it in. */
  register(username: string, password: string, signal?: AbortSignal): Promise<AuthResponse>;
  /** The current session, or null when signed out (401). */
  me(signal?: AbortSignal): Promise<AuthResponse | null>;
  logout(csrfToken: string): Promise<void>;
  session(options: SessionOptions): SessionApi;
}

export function createApiClient(fetchImpl?: FetchLike): ApiClient {
  const doFetch: FetchLike = fetchImpl ?? ((input, init) => fetch(input, init));

  return {
    async login(username, password, signal) {
      return required(
        await call<AuthResponse>(doFetch, "/api/auth/login", {
          method: "POST",
          body: { username, password },
          signal,
        }),
      );
    },

    async register(username, password, signal) {
      return required(
        await call<AuthResponse>(doFetch, "/api/auth/register", {
          method: "POST",
          body: { username, password },
          signal,
        }),
      );
    },

    async me(signal) {
      try {
        return required(await call<AuthResponse>(doFetch, "/api/auth/me", { signal }));
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) return null;
        throw error;
      }
    },

    async logout(csrfToken) {
      try {
        await call<null>(doFetch, "/api/auth/logout", {
          method: "POST",
          body: {},
          csrfToken,
          timeoutMs: LOGOUT_TIMEOUT_MS,
        });
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) return; // already signed out
        throw error;
      }
    },

    session(options) {
      async function sessionCall<T>(
        path: string,
        init: Pick<CallOptions, "method" | "body" | "timeoutMs"> = {},
      ): Promise<CallResult<T>> {
        try {
          return await call<T>(doFetch, path, {
            ...init,
            signal: options.signal,
            csrfToken: init.method === "POST" ? options.csrfToken() : undefined,
          });
        } catch (error) {
          if (error instanceof ApiError && error.status === 401) {
            options.onUnauthorized();
            throw new SessionEndedError();
          }
          throw error;
        }
      }

      return {
        async postChat(body) {
          const result = await sessionCall<ChatResponse | ChatAccepted>("/api/chat", {
            method: "POST",
            body,
            timeoutMs: CHAT_POST_TIMEOUT_MS,
          });
          const data = required(result);
          return result.status === 202
            ? { kind: "accepted", accepted: data as ChatAccepted }
            : { kind: "completed", response: data as ChatResponse };
        },
        async getChatRequest(requestId) {
          return required(await sessionCall<ChatRequestStatus>(`/api/chat/requests/${encodeURIComponent(requestId)}`));
        },
        async listConversations() {
          return required(await sessionCall<ConversationList>("/api/conversations"));
        },
        async getConversation(conversationId) {
          return required(
            await sessionCall<ConversationDetail>(`/api/conversations/${encodeURIComponent(conversationId)}`),
          );
        },
        async startRefresh() {
          return required(await sessionCall<RefreshJob>("/api/data/refresh", { method: "POST", body: {} }));
        },
        async getRefreshJob(jobId) {
          return required(await sessionCall<RefreshJob>(`/api/data/refresh/${encodeURIComponent(jobId)}`));
        },
      };
    },
  };
}
