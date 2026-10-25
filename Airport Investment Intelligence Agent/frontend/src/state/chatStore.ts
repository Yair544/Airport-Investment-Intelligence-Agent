// In-memory chat state for one signed-in session: the conversation list and one thread per opened
// or new conversation. Pure reducer and selectors; the async work lives in useChatController.

import type { OutgoingRequest, RequestFailure, RetryMode } from "../lib/chatRequest";
import type { ChatResponse, ConversationDetail, ConversationSummary } from "../types";

export type RequestPhase = "sending" | "waiting" | "queued" | "completed" | "failed" | "interrupted" | "error";

export function isPendingPhase(phase: RequestPhase): boolean {
  return phase === "sending" || phase === "waiting" || phase === "queued";
}

export function isFailedPhase(phase: RequestPhase): boolean {
  return phase === "failed" || phase === "interrupted" || phase === "error";
}

export interface RequestError {
  code: string;
  message: string;
  retryAfterSeconds: number | null;
  httpStatus: number | null;
}

export interface UserRequestState {
  requestId: string;
  sentConversationId: string | null;
  phase: RequestPhase;
  /** The server has stored this message, so it is part of the saved conversation. */
  registered: boolean;
  retry: RetryMode;
  error: RequestError | null;
  /** Start of the current attempt (ms since epoch) for the elapsed-time indicator. */
  startedAt: number | null;
}

export interface UserItem {
  kind: "user";
  key: string;
  content: string;
  request: UserRequestState | null;
}

export interface AssistantItem {
  kind: "assistant";
  key: string;
  content: string;
  response: ChatResponse | null;
}

export type ChatItem = UserItem | AssistantItem;

export interface Thread {
  threadId: string;
  conversationId: string | null;
  title: string | null;
  items: ChatItem[];
  load: "new" | "loading" | "loaded" | "error";
  loadError: string | null;
  /** Composer text for this thread. */
  draft: string;
}

export interface ChatState {
  threads: Record<string, Thread>;
  activeThreadId: string;
  conversations: ConversationSummary[];
  conversationsStatus: "loading" | "loaded" | "error";
  conversationsError: string | null;
}

export type ChatAction =
  | { type: "newThread"; threadId: string; draft?: string }
  | { type: "selectThread"; threadId: string }
  | { type: "openConversation"; threadId: string; conversationId: string; title: string | null }
  | { type: "loadStarted"; threadId: string }
  | { type: "loadSucceeded"; threadId: string; detail: ConversationDetail }
  | { type: "loadFailed"; threadId: string; message: string }
  | { type: "draftChanged"; threadId: string; draft: string }
  | { type: "requestStarted"; threadId: string; request: OutgoingRequest; startedAt: number }
  | { type: "requestProgress"; requestId: string; phase: "sending" | "waiting" | "queued" }
  | { type: "requestAccepted"; requestId: string; conversationId: string }
  | { type: "transcriptRefreshed"; detail: ConversationDetail }
  | { type: "requestCompleted"; requestId: string; response: ChatResponse }
  | { type: "requestFailed"; requestId: string; failure: RequestFailure }
  | { type: "conversationsLoaded"; conversations: ConversationSummary[] }
  | { type: "conversationsFailed"; message: string };

export function emptyThread(threadId: string, conversationId: string | null = null, title: string | null = null): Thread {
  return {
    threadId,
    conversationId,
    title,
    items: [],
    load: conversationId === null ? "new" : "loading",
    loadError: null,
    draft: "",
  };
}

export function initialChatState(threadId: string): ChatState {
  return {
    threads: { [threadId]: emptyThread(threadId) },
    activeThreadId: threadId,
    conversations: [],
    conversationsStatus: "loading",
    conversationsError: null,
  };
}

/** A new thread nothing was sent from; it can be dropped when the user moves elsewhere. */
function isBlank(thread: Thread): boolean {
  return thread.conversationId === null && thread.items.length === 0;
}

function sameText(a: string, b: string): boolean {
  return a.trim() === b.trim();
}

function replaceAt<T>(items: readonly T[], index: number, value: T): T[] {
  const next = items.slice();
  next[index] = value;
  return next;
}

function withRequest(item: UserItem, patch: Partial<UserRequestState>): UserItem {
  return item.request ? { ...item, request: { ...item.request, ...patch } } : item;
}

function parseTime(value: string): number | null {
  const time = Date.parse(value);
  return Number.isNaN(time) ? null : time;
}

/** Saved messages in `seq` order; a request still processing is shown as waiting for its answer. */
export function itemsFromDetail(detail: ConversationDetail): ChatItem[] {
  return [...detail.messages]
    .sort((a, b) => a.seq - b.seq)
    .map((message): ChatItem => {
      if (message.role === "assistant") {
        return { kind: "assistant", key: `m:${message.seq}`, content: message.content, response: message.response };
      }
      const request = message.request;
      return {
        kind: "user",
        key: request ? `u:${request.request_id}` : `m:${message.seq}`,
        content: message.content,
        request: request
          ? {
              requestId: request.request_id,
              sentConversationId: request.sent_conversation_id,
              phase: request.status === "processing" ? "waiting" : request.status,
              registered: true,
              retry: "same",
              error: null,
              startedAt: request.status === "processing" ? parseTime(message.created_at) : null,
            }
          : null,
      };
    });
}

/**
 * Server transcript plus what only this tab knows: the live phase of a request the server still
 * reports as processing, the explanation of a failure, and messages the server does not have yet
 * (pending, or rejected before they were stored).
 */
export function mergeItems(server: readonly ChatItem[], local: readonly ChatItem[]): ChatItem[] {
  const known = new Map<string, UserRequestState>();
  for (const item of local) {
    if (item.kind === "user" && item.request) known.set(item.request.requestId, item.request);
  }
  const serverIds = new Set<string>();
  const merged = server.map((item): ChatItem => {
    if (item.kind !== "user" || !item.request) return item;
    serverIds.add(item.request.requestId);
    const live = known.get(item.request.requestId);
    if (!live) return item;
    if (item.request.phase === "waiting" && isPendingPhase(live.phase)) {
      return withRequest(item, { phase: live.phase, startedAt: live.startedAt ?? item.request.startedAt });
    }
    if (isFailedPhase(item.request.phase) && isFailedPhase(live.phase)) {
      return withRequest(item, { retry: live.retry, error: live.error });
    }
    return item;
  });
  const extras = local.filter(
    (item): item is UserItem =>
      item.kind === "user" &&
      item.request !== null &&
      !serverIds.has(item.request.requestId) &&
      (isPendingPhase(item.request.phase) || !item.request.registered),
  );
  return [...merged, ...extras];
}

function sortConversations(conversations: readonly ConversationSummary[]): ConversationSummary[] {
  const time = (value: string) => {
    const parsed = Date.parse(value);
    return Number.isNaN(parsed) ? 0 : parsed;
  };
  return [...conversations].sort((a, b) => time(b.updated_at) - time(a.updated_at));
}

function updateThread(state: ChatState, threadId: string, update: (thread: Thread) => Thread): ChatState {
  const thread = state.threads[threadId];
  if (!thread) return state;
  const next = update(thread);
  return next === thread ? state : { ...state, threads: { ...state.threads, [threadId]: next } };
}

/** Applies an update to every thread that holds the request (the same conversation can be open twice). */
function updateRequest(
  state: ChatState,
  requestId: string,
  update: (thread: Thread, index: number, item: UserItem) => Thread,
): ChatState {
  let threads: Record<string, Thread> | null = null;
  for (const [id, thread] of Object.entries(state.threads)) {
    const index = thread.items.findIndex((item) => item.kind === "user" && item.request?.requestId === requestId);
    const item = thread.items[index];
    if (index < 0 || item?.kind !== "user") continue;
    const next = update(thread, index, item);
    if (next !== thread) {
      threads ??= { ...state.threads };
      threads[id] = next;
    }
  }
  return threads ? { ...state, threads } : state;
}

function withoutBlankThreads(threads: Record<string, Thread>, keep: string): Record<string, Thread> {
  const result: Record<string, Thread> = {};
  for (const [id, thread] of Object.entries(threads)) {
    if (id === keep || !isBlank(thread)) result[id] = thread;
  }
  return result;
}

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case "newThread": {
      const active = state.threads[state.activeThreadId];
      if (active && isBlank(active) && !action.draft) return state;
      const threads = withoutBlankThreads(state.threads, action.threadId);
      threads[action.threadId] = { ...emptyThread(action.threadId), draft: action.draft ?? "" };
      return { ...state, threads, activeThreadId: action.threadId };
    }

    case "selectThread":
      if (!state.threads[action.threadId]) return state;
      return { ...state, threads: withoutBlankThreads(state.threads, action.threadId), activeThreadId: action.threadId };

    case "openConversation": {
      const existing = state.threads[action.threadId];
      const threads = withoutBlankThreads(state.threads, action.threadId);
      threads[action.threadId] = existing
        ? { ...existing, title: existing.title ?? action.title }
        : emptyThread(action.threadId, action.conversationId, action.title);
      return { ...state, threads, activeThreadId: action.threadId };
    }

    case "loadStarted":
      return updateThread(state, action.threadId, (thread) => ({ ...thread, load: "loading", loadError: null }));

    case "loadSucceeded":
      return updateThread(state, action.threadId, (thread) => ({
        ...thread,
        conversationId: action.detail.conversation_id,
        title: action.detail.title || thread.title,
        items: mergeItems(itemsFromDetail(action.detail), thread.items),
        load: "loaded",
        loadError: null,
      }));

    case "loadFailed":
      return updateThread(state, action.threadId, (thread) => ({ ...thread, load: "error", loadError: action.message }));

    case "draftChanged":
      return updateThread(state, action.threadId, (thread) =>
        thread.draft === action.draft ? thread : { ...thread, draft: action.draft },
      );

    case "requestStarted":
      return updateThread(state, action.threadId, (thread) => {
        const { request, startedAt } = action;
        const index = thread.items.findIndex(
          (item) => item.kind === "user" && item.request?.requestId === request.requestId,
        );
        const existing = thread.items[index];
        if (existing?.kind === "user") {
          // Retry of the same request: reuse the message bubble instead of adding a new one.
          return {
            ...thread,
            items: replaceAt(thread.items, index, withRequest(existing, { phase: "sending", error: null, startedAt })),
            draft: sameText(thread.draft, existing.content) ? "" : thread.draft,
          };
        }
        // Messages the server never stored disappear once a different message is sent.
        const kept = thread.items.filter(
          (item) => !(item.kind === "user" && item.request && !item.request.registered && isFailedPhase(item.request.phase)),
        );
        const item: UserItem = {
          kind: "user",
          key: `u:${request.requestId}`,
          content: request.message,
          request: {
            requestId: request.requestId,
            sentConversationId: request.sentConversationId,
            phase: "sending",
            registered: false,
            retry: "same",
            error: null,
            startedAt,
          },
        };
        return { ...thread, items: [...kept, item], draft: sameText(thread.draft, request.message) ? "" : thread.draft };
      });

    case "requestProgress":
      return updateRequest(state, action.requestId, (thread, index, item) =>
        item.request && isPendingPhase(item.request.phase) && item.request.phase !== action.phase
          ? { ...thread, items: replaceAt(thread.items, index, withRequest(item, { phase: action.phase })) }
          : thread,
      );

    case "requestAccepted":
      return updateRequest(state, action.requestId, (thread, index, item) => ({
        ...thread,
        conversationId: thread.conversationId ?? action.conversationId,
        items: replaceAt(thread.items, index, withRequest(item, { registered: true })),
      }));

    case "transcriptRefreshed": {
      const conversationId = action.detail.conversation_id;
      let threads: Record<string, Thread> | null = null;
      for (const [id, thread] of Object.entries(state.threads)) {
        if (thread.conversationId !== conversationId) continue;
        threads ??= { ...state.threads };
        threads[id] = {
          ...thread,
          title: action.detail.title || thread.title,
          items: mergeItems(itemsFromDetail(action.detail), thread.items),
        };
      }
      return threads ? { ...state, threads } : state;
    }

    case "requestCompleted":
      return updateRequest(state, action.requestId, (thread, index, item) => {
        const answer: AssistantItem = {
          kind: "assistant",
          key: `a:${action.requestId}`,
          content: action.response.answer,
          response: action.response,
        };
        let items = replaceAt(
          thread.items,
          index,
          withRequest(item, { phase: "completed", registered: true, error: null, startedAt: null }),
        );
        const next = items[index + 1];
        if (next?.kind === "assistant") {
          if (next.key === answer.key) items = replaceAt(items, index + 1, answer); // else: already loaded from the server
        } else {
          items = [...items.slice(0, index + 1), answer, ...items.slice(index + 1)];
        }
        return { ...thread, items, conversationId: thread.conversationId ?? action.response.conversation_id };
      });

    case "requestFailed":
      return updateRequest(state, action.requestId, (thread, index, item) => {
        const { failure } = action;
        const registered = (item.request?.registered ?? false) || failure.registered;
        const items = replaceAt(
          thread.items,
          index,
          withRequest(item, {
            phase: failure.status,
            registered,
            retry: failure.retry,
            error: {
              code: failure.code,
              message: failure.message,
              retryAfterSeconds: failure.retryAfterSeconds,
              httpStatus: failure.httpStatus,
            },
            startedAt: null,
          }),
        );
        return {
          ...thread,
          items,
          conversationId: thread.conversationId ?? (registered ? failure.conversationId : null),
          // Keep the typed text available in the composer unless the user already typed something new.
          draft: thread.draft.trim() === "" ? item.content : thread.draft,
        };
      });

    case "conversationsLoaded":
      return {
        ...state,
        conversations: sortConversations(action.conversations),
        conversationsStatus: "loaded",
        conversationsError: null,
      };

    case "conversationsFailed":
      return { ...state, conversationsStatus: "error", conversationsError: action.message };

    default:
      return state;
  }
}

// ---------------------------------------------------------------------------------------------
// Selectors
// ---------------------------------------------------------------------------------------------

export function selectActiveThread(state: ChatState): Thread {
  return state.threads[state.activeThreadId] ?? emptyThread(state.activeThreadId);
}

export function lastUserItem(thread: Thread): UserItem | undefined {
  for (let index = thread.items.length - 1; index >= 0; index -= 1) {
    const item = thread.items[index];
    if (item?.kind === "user") return item;
  }
  return undefined;
}

/** The request this thread is waiting for, if any; while it exists, Send stays disabled. */
export function pendingItem(thread: Thread): UserItem | undefined {
  return thread.items.find(
    (item): item is UserItem => item.kind === "user" && item.request !== null && isPendingPhase(item.request.phase),
  );
}

/** Only the latest message can be retried; once the conversation moved on the server rejects it. */
export function canRetryItem(thread: Thread, item: UserItem): boolean {
  return (
    item.request !== null &&
    isFailedPhase(item.request.phase) &&
    item.request.retry === "same" &&
    lastUserItem(thread) === item &&
    pendingItem(thread) === undefined
  );
}

/** Sending exactly the text of the retryable last message resends that request instead of a duplicate. */
export function retryCandidate(thread: Thread, message: string): UserItem | undefined {
  const last = lastUserItem(thread);
  return last && canRetryItem(thread, last) && sameText(last.content, message) ? last : undefined;
}

export function findThreadByConversation(state: ChatState, conversationId: string): Thread | undefined {
  const matches = Object.values(state.threads).filter((thread) => thread.conversationId === conversationId);
  return matches.find((thread) => pendingItem(thread) !== undefined) ?? matches[0];
}

export interface SidebarEntry {
  key: string;
  threadId: string | null;
  conversationId: string | null;
  title: string;
  updatedAt: string | null;
  pending: boolean;
  active: boolean;
}

function firstUserText(thread: Thread): string | null {
  const first = thread.items.find((item) => item.kind === "user");
  return first ? first.content : null;
}

/** Unsaved threads first, then conversations the list does not show yet, then the saved list. */
export function sidebarEntries(state: ChatState): SidebarEntry[] {
  const active = state.threads[state.activeThreadId];
  const listed = new Set(state.conversations.map((conversation) => conversation.conversation_id));
  const local: SidebarEntry[] = [];
  for (const thread of Object.values(state.threads)) {
    if (thread.items.length === 0) continue;
    if (thread.conversationId !== null && listed.has(thread.conversationId)) continue;
    if (thread.conversationId !== null) listed.add(thread.conversationId);
    local.push({
      key: `t:${thread.threadId}`,
      threadId: thread.threadId,
      conversationId: thread.conversationId,
      title: thread.title ?? firstUserText(thread) ?? "New conversation",
      updatedAt: null,
      pending: pendingItem(thread) !== undefined,
      active: thread.threadId === state.activeThreadId,
    });
  }
  const saved = state.conversations.map((conversation): SidebarEntry => {
    const thread = findThreadByConversation(state, conversation.conversation_id);
    return {
      key: `c:${conversation.conversation_id}`,
      threadId: thread?.threadId ?? null,
      conversationId: conversation.conversation_id,
      title: conversation.title || "Untitled conversation",
      updatedAt: conversation.updated_at,
      pending: thread ? pendingItem(thread) !== undefined : false,
      active: active?.conversationId === conversation.conversation_id,
    };
  });
  return [...local, ...saved];
}
