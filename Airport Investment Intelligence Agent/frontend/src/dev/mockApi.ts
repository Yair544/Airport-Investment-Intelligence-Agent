// Development only (`npm run dev`, then open /?demo): an in-memory imitation of the backend API so
// the interface can be exercised without FastAPI. Loaded through a dynamic import that is guarded by
// import.meta.env.DEV, so it is not part of production builds.
//
// Messages containing "fail" time out once (504) and succeed on Retry; "slow" returns 202 and
// completes later; Hebrew text gets a Hebrew answer. Password "wrong" fails sign-in.

import type { FetchLike } from "../api/client";
import type {
  ChatRequestStatus,
  ChatResponse,
  ConversationDetail,
  ErrorBody,
  MessageOut,
  RefreshJob,
  User,
} from "../types";
import { sampleResponses } from "./mockData";

interface MockRequest {
  request_id: string;
  conversation_id: string;
  sent_conversation_id: string | null;
  message: string;
  status: ChatRequestStatus["status"];
  created_at: string;
  updated_at: string;
  response: ChatResponse | null;
  error: ErrorBody | null;
  readyAt: number | null;
  failedOnce: boolean;
}

interface MockConversation {
  conversation_id: string;
  title: string;
  created_at: string;
  updated_at: string;
  messages: MessageOut[];
}

const iso = (ms: number = Date.now()) => new Date(ms).toISOString();

function json(status: number, body?: unknown, headers: Record<string, string> = {}): Response {
  if (status === 204) return new Response(null, { status });
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
}

function error(status: number, code: string, message: string, extra: Partial<ErrorBody> = {}): Response {
  return json(status, { error: { code, message, ...extra } });
}

function delay(ms: number, signal: AbortSignal | null | undefined): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason);
      return;
    }
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => {
      clearTimeout(timer);
      reject(signal.reason);
    });
  });
}

function pickResponse(message: string): ChatResponse {
  const text = message.toLowerCase();
  if (/[֐-׿]/.test(message)) return sampleResponses.hebrew;
  if (text.includes("compare") || text.includes("congestion") || text.includes("santa ana")) return sampleResponses.comparison;
  if (text.includes("long haul") || text.includes("long-haul") || text.includes("anchorage")) return sampleResponses.longHaul;
  if (text.includes("unmet") || text.includes("sfo")) return sampleResponses.unmetDemand;
  if (text.includes("new england") || text.includes("rank") || text.includes("expansion") || text.includes("candidate")) {
    return sampleResponses.ranking;
  }
  return sampleResponses.clarification;
}

export function createMockFetch(): FetchLike {
  let user: User | null = null;
  let csrf = "";
  let refreshRuns = 0;
  let job: (RefreshJob & { startedMs: number }) | null = null;
  const conversations = new Map<string, MockConversation>();
  const requests = new Map<string, MockRequest>();

  const addMessage = (conversation: MockConversation, message: Omit<MessageOut, "seq">) => {
    conversation.messages.push({ ...message, seq: conversation.messages.length + 1 });
    conversation.updated_at = message.created_at;
  };

  const complete = (request: MockRequest, at: number = Date.now()) => {
    const conversation = conversations.get(request.conversation_id);
    if (!conversation || request.status === "completed") return;
    const answer = { ...pickResponse(request.message), conversation_id: request.conversation_id };
    request.status = "completed";
    request.response = answer;
    request.updated_at = iso(at);
    request.readyAt = null;
    addMessage(conversation, { role: "assistant", content: answer.answer, created_at: iso(at), request: null, response: answer });
  };

  const syncUserMessage = (request: MockRequest) => {
    const message = conversations
      .get(request.conversation_id)
      ?.messages.find((item) => item.request?.request_id === request.request_id);
    if (message?.request) message.request = { ...message.request, status: request.status };
  };

  const settle = (request: MockRequest) => {
    if (request.status === "processing" && request.readyAt !== null && Date.now() >= request.readyAt) complete(request);
    syncUserMessage(request);
  };

  const seed = () => {
    const day = 24 * 3600 * 1000;
    const make = (id: string, title: string, age: number) => {
      const conversation: MockConversation = { conversation_id: id, title, created_at: iso(Date.now() - age), updated_at: iso(Date.now() - age), messages: [] };
      conversations.set(id, conversation);
      return conversation;
    };
    const ask = (conversation: MockConversation, requestId: string, status: MockRequest["status"], readyAt: number | null) => {
      const request: MockRequest = {
        request_id: requestId,
        conversation_id: conversation.conversation_id,
        sent_conversation_id: null,
        message: conversation.title,
        status: "processing",
        created_at: conversation.created_at,
        updated_at: conversation.created_at,
        response: null,
        error: null,
        readyAt,
        failedOnce: true,
      };
      requests.set(requestId, request);
      addMessage(conversation, {
        role: "user",
        content: conversation.title,
        created_at: conversation.created_at,
        request: { request_id: requestId, status: "processing", sent_conversation_id: null },
        response: null,
      });
      if (status === "completed") complete(request, Date.parse(conversation.created_at) + 30_000);
      if (status === "interrupted") {
        request.status = "interrupted";
        request.error = { code: "interrupted", message: "Processing stopped before an answer was saved. You can retry the same request." };
      }
      syncUserMessage(request);
    };
    ask(make("conv_seed_compare", "Compare LA and Santa Ana airport congestion levels.", 2 * day), "11111111-1111-4111-8111-111111111111", "completed", null);
    ask(make("conv_seed_sfo", "What is the unmet flight demand in SFO airport and why?", day), "22222222-2222-4222-8222-222222222222", "interrupted", null);
    ask(make("conv_seed_ne", "Which airports in New England are strong candidates for terminal expansion?", 60_000), "33333333-3333-4333-8333-333333333333", "processing", Date.now() + 8000);
  };
  seed();

  const status = (request: MockRequest): ChatRequestStatus => ({
    request_id: request.request_id,
    conversation_id: request.conversation_id,
    status: request.status,
    created_at: request.created_at,
    updated_at: request.updated_at,
    response: request.status === "completed" ? request.response : null,
    error: request.error,
  });

  const detail = (conversation: MockConversation): ConversationDetail => ({ ...conversation, messages: [...conversation.messages] });

  const refreshJob = (): RefreshJob => {
    if (!job) throw new Error("no job");
    const elapsed = Date.now() - job.startedMs;
    const stages: Array<[number, string]> = [
      [2000, "checking sources"],
      [4500, "downloading"],
      [7000, "validating coverage"],
      [9000, "national references"],
    ];
    if (job.status === "queued" || job.status === "running") {
      const stage = stages.find(([until]) => elapsed < until);
      if (elapsed < 1000) Object.assign(job, { status: "queued", stage: "queued" });
      else if (stage) Object.assign(job, { status: "running", stage: stage[1], started_at: job.started_at ?? iso() });
      else {
        const changed = refreshRuns % 2 === 0;
        Object.assign(job, {
          status: "completed",
          stage: "completed",
          finished_at: iso(),
          changed,
          active_version_id: changed ? "dv_demo_2026_11" : "dv_demo_2026_10",
          sources: ["faa_taf", "bts_t100_segment", "bts_ontime", "bts_t100_summary"].map((source_id, index) => ({
            source_id,
            status: changed && index === 1 ? "updated" : "unchanged",
            periods: source_id === "faa_taf" ? ["FY2024–FY2045"] : ["2024-01 to 2025-12"],
            checked_at: iso(),
            retrieved_at: changed && index === 1 ? iso() : "2026-10-20T08:30:00Z",
            message: null,
          })),
        });
      }
    }
    const { startedMs: _ignored, ...rest } = job;
    void _ignored;
    return rest;
  };

  return async (input, init) => {
    const url = new URL(input, window.location.origin);
    const path = url.pathname;
    const method = (init.method ?? "GET").toUpperCase();
    const headers = new Headers(init.headers);
    const body: Record<string, unknown> = init.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : {};
    await delay(150, init.signal);

    if (path === "/api/auth/login" && method === "POST") {
      await delay(300, init.signal);
      if (body.password === "wrong" || !body.username) return error(401, "invalid_credentials", "Invalid username or password.");
      user = { user_id: `usr_${String(body.username)}`, username: String(body.username) };
      csrf = crypto.randomUUID();
      return json(200, { user, csrf_token: csrf });
    }
    if (path === "/api/auth/register" && method === "POST") {
      await delay(300, init.signal);
      if (body.username === "analyst") return error(409, "username_taken", "A user with this username already exists.");
      user = { user_id: `usr_${String(body.username)}`, username: String(body.username) };
      csrf = crypto.randomUUID();
      return json(200, { user, csrf_token: csrf });
    }
    if (path === "/api/auth/me") return user ? json(200, { user, csrf_token: csrf }) : error(401, "not_authenticated", "Sign in to continue.");
    if (!user) return error(401, "not_authenticated", "Sign in to continue.");
    if (method === "POST" && headers.get("X-CSRF-Token") !== csrf) return error(403, "csrf_failed", "The security token is missing or invalid.");

    if (path === "/api/auth/logout") {
      user = null;
      return json(204);
    }

    if (path === "/api/conversations") {
      const list = [...conversations.values()]
        .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
        .map(({ conversation_id, title, updated_at }) => ({ conversation_id, title, updated_at }));
      return json(200, { conversations: list });
    }
    const conversationMatch = /^\/api\/conversations\/([^/]+)$/.exec(path);
    if (conversationMatch) {
      const conversation = conversations.get(decodeURIComponent(conversationMatch[1] ?? ""));
      if (!conversation) return error(404, "conversation_not_found", "Conversation not found.");
      for (const request of requests.values()) if (request.conversation_id === conversation.conversation_id) settle(request);
      return json(200, detail(conversation));
    }

    const requestMatch = /^\/api\/chat\/requests\/([^/]+)$/.exec(path);
    if (requestMatch) {
      const request = requests.get(decodeURIComponent(requestMatch[1] ?? ""));
      if (!request) return error(404, "request_not_found", "Request not found.");
      settle(request);
      return json(200, status(request));
    }

    if (path === "/api/chat" && method === "POST") {
      const requestId = String(body.request_id);
      const message = String(body.message ?? "");
      const sentConversationId = (body.conversation_id as string | null) ?? null;
      let request = requests.get(requestId);
      if (request) {
        if (request.message !== message || request.sent_conversation_id !== sentConversationId) {
          return error(409, "request_id_conflict", "This request id was already used with different content.");
        }
        settle(request);
        if (request.status === "completed") return json(200, request.response);
        if (request.status === "processing") {
          return json(202, { request_id: requestId, conversation_id: request.conversation_id, status: "processing", status_url: `/api/chat/requests/${requestId}` });
        }
        request.status = "processing";
        request.error = null;
        request.updated_at = iso();
        syncUserMessage(request);
      } else {
        let conversation: MockConversation | undefined;
        if (sentConversationId !== null) {
          conversation = conversations.get(sentConversationId);
          if (!conversation) return error(404, "conversation_not_found", "Conversation not found.");
          const busy = [...requests.values()].some((r) => r.conversation_id === sentConversationId && r.status === "processing");
          if (busy) return error(409, "conversation_busy", "Another request in this conversation is still processing.", { conversation_id: sentConversationId });
        } else {
          const id = `conv_${crypto.randomUUID().slice(0, 8)}`;
          conversation = { conversation_id: id, title: message.slice(0, 80), created_at: iso(), updated_at: iso(), messages: [] };
          conversations.set(id, conversation);
        }
        request = {
          request_id: requestId,
          conversation_id: conversation.conversation_id,
          sent_conversation_id: sentConversationId,
          message,
          status: "processing",
          created_at: iso(),
          updated_at: iso(),
          response: null,
          error: null,
          readyAt: null,
          failedOnce: false,
        };
        requests.set(requestId, request);
        addMessage(conversation, {
          role: "user",
          content: message,
          created_at: iso(),
          request: { request_id: requestId, status: "processing", sent_conversation_id: sentConversationId },
          response: null,
        });
      }

      const lower = message.toLowerCase();
      if (lower.includes("slow")) {
        request.readyAt = Date.now() + 6000;
        return json(202, { request_id: requestId, conversation_id: request.conversation_id, status: "processing", status_url: `/api/chat/requests/${requestId}` });
      }
      await delay(lower.includes("fail") ? 1500 : 2500, init.signal);
      if (lower.includes("fail") && !request.failedOnce) {
        request.failedOnce = true;
        request.status = "failed";
        request.error = { code: "processing_timeout", message: "The analysis took too long and was stopped. You can retry the same request." };
        request.updated_at = iso();
        syncUserMessage(request);
        return error(504, "processing_timeout", request.error.message, { request_id: requestId, conversation_id: request.conversation_id });
      }
      complete(request);
      syncUserMessage(request);
      return json(200, request.response);
    }

    if (path === "/api/data/refresh" && method === "POST") {
      if (!job || (job.status !== "queued" && job.status !== "running")) {
        refreshRuns += 1;
        const id = `job_${crypto.randomUUID().slice(0, 8)}`;
        job = {
          job_id: id,
          status: "queued",
          stage: "queued",
          created_at: iso(),
          started_at: null,
          finished_at: null,
          sources: [],
          changed: null,
          active_version_id: null,
          error: null,
          status_url: `/api/data/refresh/${id}`,
          startedMs: Date.now(),
        };
      }
      return json(202, refreshJob());
    }
    const jobMatch = /^\/api\/data\/refresh\/([^/]+)$/.exec(path);
    if (jobMatch) {
      if (!job || job.job_id !== decodeURIComponent(jobMatch[1] ?? "")) return error(404, "job_not_found", "Refresh job not found.");
      return json(200, refreshJob());
    }

    return error(404, "not_found", "Not found.");
  };
}
