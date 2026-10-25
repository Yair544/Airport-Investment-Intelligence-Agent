import { describe, expect, it } from "vitest";
import { sampleResponses } from "../dev/mockData";
import type { RequestFailure } from "../lib/chatRequest";
import type { ConversationDetail } from "../types";
import {
  canRetryItem,
  chatReducer,
  initialChatState,
  lastUserItem,
  pendingItem,
  retryCandidate,
  sidebarEntries,
  type ChatAction,
  type ChatState,
  type UserItem,
} from "./chatStore";

const T = "n:thread";

function run(state: ChatState, ...actions: ChatAction[]): ChatState {
  return actions.reduce(chatReducer, state);
}

function start(message: string, requestId: string, sentConversationId: string | null = null): ChatAction {
  return { type: "requestStarted", threadId: T, request: { requestId, message, sentConversationId }, startedAt: 1000 };
}

function failure(extra: Partial<RequestFailure> = {}): RequestFailure {
  return {
    status: "failed",
    code: "provider_error",
    message: "The model provider failed.",
    retry: "same",
    registered: true,
    conversationId: "conv-1",
    httpStatus: null,
    retryAfterSeconds: null,
    ...extra,
  };
}

const thread = (state: ChatState) => state.threads[T]!;
const users = (state: ChatState) => thread(state).items.filter((item): item is UserItem => item.kind === "user");

describe("sending", () => {
  it("adds the message as pending and clears the composer text that was sent", () => {
    const state = run(initialChatState(T), { type: "draftChanged", threadId: T, draft: "  Compare LAX and SNA " }, start("Compare LAX and SNA", "r1"));
    expect(thread(state).draft).toBe("");
    expect(users(state)).toHaveLength(1);
    expect(users(state)[0]?.request).toMatchObject({ requestId: "r1", phase: "sending", sentConversationId: null, registered: false });
    expect(pendingItem(thread(state))?.request?.requestId).toBe("r1");
  });

  it("keeps an unrelated draft when a suggestion is sent", () => {
    const state = run(initialChatState(T), { type: "draftChanged", threadId: T, draft: "my own question" }, start("Suggested question", "r1"));
    expect(thread(state).draft).toBe("my own question");
  });

  it("inserts the answer right after its question and adopts the new conversation id", () => {
    const answer = { ...sampleResponses.comparison, conversation_id: "conv-new" };
    const completed: ChatAction = { type: "requestCompleted", requestId: "r1", response: answer };
    const state = run(initialChatState(T), start("Compare", "r1"), completed, completed);
    expect(thread(state).conversationId).toBe("conv-new");
    expect(thread(state).items.map((item) => item.kind)).toEqual(["user", "assistant"]);
    expect(users(state)[0]?.request?.phase).toBe("completed");
    expect(pendingItem(thread(state))).toBeUndefined();
  });

  it("does not move a finished request back to a pending phase", () => {
    const state = run(
      initialChatState(T),
      start("Compare", "r1"),
      { type: "requestCompleted", requestId: "r1", response: sampleResponses.comparison },
      { type: "requestProgress", requestId: "r1", phase: "waiting" },
    );
    expect(users(state)[0]?.request?.phase).toBe("completed");
  });
});

describe("failures and retry", () => {
  it("keeps the typed text in the composer after an error", () => {
    const state = run(initialChatState(T), start("Compare LAX and SNA", "r1"), { type: "requestFailed", requestId: "r1", failure: failure() });
    expect(thread(state).draft).toBe("Compare LAX and SNA");
    expect(users(state)[0]?.request).toMatchObject({ phase: "failed", retry: "same", error: { message: "The model provider failed." } });
  });

  it("does not overwrite text typed while the request was pending", () => {
    const state = run(
      initialChatState(T),
      start("First question", "r1"),
      { type: "draftChanged", threadId: T, draft: "Next question" },
      { type: "requestFailed", requestId: "r1", failure: failure() },
    );
    expect(thread(state).draft).toBe("Next question");
  });

  it("adopts the conversation created by a stored first message that then failed", () => {
    const state = run(
      initialChatState(T),
      start("First", "r1"),
      { type: "requestFailed", requestId: "r1", failure: failure({ status: "error", code: "processing_timeout", conversationId: "conv-7" }) },
    );
    expect(thread(state).conversationId).toBe("conv-7");
    expect(users(state)[0]?.request?.registered).toBe(true);
  });

  it("retries the same request in place and treats sending the same text as a retry", () => {
    let state = run(initialChatState(T), start("Compare", "r1"), { type: "requestFailed", requestId: "r1", failure: failure() });
    const item = lastUserItem(thread(state))!;
    expect(canRetryItem(thread(state), item)).toBe(true);
    expect(retryCandidate(thread(state), " Compare ")).toBe(item);
    expect(retryCandidate(thread(state), "Something else")).toBeUndefined();

    state = run(state, start("Compare", "r1", null));
    expect(users(state)).toHaveLength(1);
    expect(users(state)[0]?.request).toMatchObject({ phase: "sending", error: null });
    expect(thread(state).draft).toBe("");
  });

  it("only allows a retry for the latest message", () => {
    const state = run(
      initialChatState(T),
      start("First", "r1"),
      { type: "requestFailed", requestId: "r1", failure: failure() },
      start("Second", "r2", "conv-1"),
      { type: "requestCompleted", requestId: "r2", response: sampleResponses.clarification },
    );
    const first = users(state)[0]!;
    expect(first.request?.registered).toBe(true);
    expect(canRetryItem(thread(state), first)).toBe(false);
  });

  it("drops messages the server never stored once a different message is sent", () => {
    const state = run(
      initialChatState(T),
      start("Too fast", "r1"),
      { type: "requestFailed", requestId: "r1", failure: failure({ status: "error", code: "rate_limited", registered: false, conversationId: null }) },
      start("Another question", "r2"),
    );
    expect(users(state).map((item) => item.request?.requestId)).toEqual(["r2"]);
  });

  it("does not offer a retry when the request must be sent as a new message", () => {
    const state = run(
      initialChatState(T),
      start("Old", "r1"),
      { type: "requestFailed", requestId: "r1", failure: failure({ status: "error", code: "stale_request", retry: "new_message" }) },
    );
    expect(canRetryItem(thread(state), lastUserItem(thread(state))!)).toBe(false);
    expect(retryCandidate(thread(state), "Old")).toBeUndefined();
  });
});

describe("loading a saved conversation", () => {
  const detail: ConversationDetail = {
    conversation_id: "conv-1",
    title: "Saved",
    created_at: "2026-10-23T09:00:00Z",
    updated_at: "2026-10-23T10:00:00Z",
    messages: [
      { seq: 3, role: "user", content: "Second", created_at: "2026-10-23T10:00:00Z", request: { request_id: "r2", status: "processing", sent_conversation_id: "conv-1" }, response: null },
      { seq: 1, role: "user", content: "First", created_at: "2026-10-23T09:00:00Z", request: { request_id: "r1", status: "completed", sent_conversation_id: null }, response: null },
      { seq: 2, role: "assistant", content: "Answer", created_at: "2026-10-23T09:01:00Z", request: null, response: sampleResponses.clarification },
    ],
  };

  it("shows messages in order and resumes waiting for an unfinished request", () => {
    const state = run(
      initialChatState(T),
      { type: "openConversation", threadId: "c:conv-1", conversationId: "conv-1", title: "Saved" },
      { type: "draftChanged", threadId: "c:conv-1", draft: "kept draft" },
      { type: "loadSucceeded", threadId: "c:conv-1", detail },
    );
    const loaded = state.threads["c:conv-1"]!;
    expect(loaded.items.map((item) => item.content)).toEqual(["First", "Answer", "Second"]);
    const last = lastUserItem(loaded)!;
    expect(last.request).toMatchObject({ phase: "waiting", registered: true, startedAt: Date.parse("2026-10-23T10:00:00Z") });
    expect(pendingItem(loaded)).toBe(last);
    expect(loaded.draft).toBe("kept draft");
    expect(state.threads[T]).toBeUndefined(); // the blank new thread was dropped
  });

  it("keeps a local pending message the server does not show yet", () => {
    const state = run(
      initialChatState(T),
      start("Queued question", "r9", "conv-1"),
      { type: "requestProgress", requestId: "r9", phase: "queued" },
    );
    // A follow-up in an existing conversation: the thread already knows its conversation id.
    const withConversation = { ...state, threads: { [T]: { ...thread(state), conversationId: "conv-1" } } };
    const refreshed = run(withConversation, { type: "transcriptRefreshed", detail });
    expect(thread(refreshed).items.map((item) => item.content)).toEqual(["First", "Answer", "Second", "Queued question"]);
    expect(lastUserItem(thread(refreshed))?.request?.phase).toBe("queued");
  });
});

describe("threads and sidebar", () => {
  it("reuses a blank new conversation instead of stacking empty ones", () => {
    const state = initialChatState(T);
    expect(run(state, { type: "newThread", threadId: "n:other" })).toBe(state);
  });

  it("lists unsaved conversations first and marks the active one", () => {
    const state = run(
      initialChatState(T),
      start("Unsaved question", "r1"),
      {
        type: "conversationsLoaded",
        conversations: [
          { conversation_id: "old", title: "Older", updated_at: "2026-10-20T10:00:00Z" },
          { conversation_id: "new", title: "Newer", updated_at: "2026-10-22T10:00:00Z" },
        ],
      },
    );
    const entries = sidebarEntries(state);
    expect(entries.map((entry) => entry.title)).toEqual(["Unsaved question", "Newer", "Older"]);
    expect(entries[0]).toMatchObject({ active: true, pending: true, conversationId: null });
  });
});
