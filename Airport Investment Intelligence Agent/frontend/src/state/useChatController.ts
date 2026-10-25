import { useCallback, useEffect, useMemo, useReducer, useRef } from "react";
import { ApiError, errorMessage, isCancellation, type SessionApi } from "../api/client";
import {
  resumeChat,
  sendChat,
  type ChatProgress,
  type OutgoingRequest,
  type RequestFailure,
} from "../lib/chatRequest";
import { newUuid } from "../lib/ids";
import type { ConversationDetail } from "../types";
import {
  canRetryItem,
  chatReducer,
  findThreadByConversation,
  initialChatState,
  pendingItem,
  retryCandidate,
  selectActiveThread,
  type ChatState,
  type Thread,
  type UserItem,
} from "./chatStore";

/** Mirrors the backend's default `max_message_chars`; the server still enforces its own limit. */
export const MAX_MESSAGE_CHARS = 4000;

export interface ChatController {
  state: ChatState;
  thread: Thread;
  send(text: string): void;
  retry(requestId: string): void;
  setDraft(draft: string): void;
  openConversation(conversationId: string): void;
  openThread(threadId: string): void;
  newConversation(draft?: string): void;
  reloadThread(): void;
  refreshConversations(): void;
}

const newThreadId = () => `n:${newUuid()}`;

function unexpectedFailure(error: unknown): RequestFailure {
  return {
    status: "error",
    code: "unexpected",
    message: errorMessage(error),
    retry: "same",
    registered: false,
    conversationId: null,
    httpStatus: null,
    retryAfterSeconds: null,
  };
}

/**
 * Chat state and request orchestration for one session. Everything is bound to the session
 * signal: once it aborts (sign-out, expiry, account switch) no result is dispatched any more.
 */
export function useChatController(api: SessionApi, signal: AbortSignal, onSessionCheck: () => void): ChatController {
  const [state, dispatch] = useReducer(chatReducer, undefined, () => initialChatState(newThreadId()));
  const stateRef = useRef(state);
  stateRef.current = state;

  const running = useRef(new Set<string>()); // request ids followed by a runner
  const starting = useRef(new Set<string>()); // threads with a send in flight (blocks double submit)
  const loadTokens = useRef(new Map<string, number>());
  const listToken = useRef(0);
  const sessionCheck = useRef(onSessionCheck);
  sessionCheck.current = onSessionCheck;

  const refreshConversations = useCallback(async () => {
    const token = ++listToken.current;
    try {
      const list = await api.listConversations();
      if (!signal.aborted && token === listToken.current) {
        dispatch({ type: "conversationsLoaded", conversations: list.conversations });
      }
    } catch (error) {
      if (isCancellation(error) || signal.aborted || token !== listToken.current) return;
      dispatch({ type: "conversationsFailed", message: errorMessage(error) });
    }
  }, [api, signal]);

  // `run` and `resumeProcessing` call each other, so the latter goes through a ref.
  const resumeRef = useRef<(detail: ConversationDetail, exceptRequestId: string | null) => void>(() => {});

  const run = useCallback(
    async (request: OutgoingRequest, mode: "send" | "resume") => {
      if (running.current.has(request.requestId)) return;
      running.current.add(request.requestId);
      const onProgress = (progress: ChatProgress) => {
        if (signal.aborted) return;
        if (progress.type === "phase") {
          dispatch({ type: "requestProgress", requestId: request.requestId, phase: progress.phase });
        } else if (progress.type === "accepted") {
          dispatch({ type: "requestAccepted", requestId: request.requestId, conversationId: progress.conversationId });
          if (request.sentConversationId === null) void refreshConversations();
        } else {
          dispatch({ type: "transcriptRefreshed", detail: progress.detail });
          resumeRef.current(progress.detail, request.requestId);
        }
      };
      try {
        const deps = { api, signal, onProgress };
        const outcome = mode === "send" ? await sendChat(request, deps) : await resumeChat(request, deps);
        if (signal.aborted) return;
        if (outcome.kind === "completed") {
          dispatch({ type: "requestCompleted", requestId: request.requestId, response: outcome.response });
        } else {
          dispatch({ type: "requestFailed", requestId: request.requestId, failure: outcome.failure });
          // A CSRF rejection or a vanished request can mean the session changed in another tab.
          if (outcome.failure.httpStatus === 403 || outcome.failure.code === "request_not_found") {
            sessionCheck.current();
          }
        }
        void refreshConversations();
      } catch (error) {
        if (isCancellation(error) || signal.aborted) return;
        dispatch({ type: "requestFailed", requestId: request.requestId, failure: unexpectedFailure(error) });
      } finally {
        running.current.delete(request.requestId);
      }
    },
    [api, signal, refreshConversations],
  );

  /** Follows every request a transcript shows as processing, so its answer appears when ready. */
  resumeRef.current = (detail, exceptRequestId) => {
    for (const message of detail.messages) {
      const request = message.role === "user" ? message.request : null;
      if (!request || request.status !== "processing" || request.request_id === exceptRequestId) continue;
      void run(
        { requestId: request.request_id, message: message.content, sentConversationId: request.sent_conversation_id },
        "resume",
      );
    }
  };

  const loadThread = useCallback(
    async (threadId: string, conversationId: string) => {
      const token = (loadTokens.current.get(threadId) ?? 0) + 1;
      loadTokens.current.set(threadId, token);
      dispatch({ type: "loadStarted", threadId });
      try {
        const detail = await api.getConversation(conversationId);
        if (signal.aborted || loadTokens.current.get(threadId) !== token) return;
        dispatch({ type: "loadSucceeded", threadId, detail });
        resumeRef.current(detail, null);
      } catch (error) {
        if (isCancellation(error) || signal.aborted || loadTokens.current.get(threadId) !== token) return;
        const notFound = error instanceof ApiError && error.status === 404;
        dispatch({
          type: "loadFailed",
          threadId,
          message: notFound ? "This conversation was not found." : errorMessage(error),
        });
        if (notFound) void refreshConversations();
      }
    },
    [api, signal, refreshConversations],
  );

  const start = useCallback(
    (threadId: string, request: OutgoingRequest) => {
      starting.current.add(threadId);
      dispatch({ type: "requestStarted", threadId, request, startedAt: Date.now() });
      void run(request, "send").finally(() => starting.current.delete(threadId));
    },
    [run],
  );

  const send = useCallback(
    (text: string) => {
      const current = stateRef.current;
      const thread = current.threads[current.activeThreadId];
      const message = text.trim();
      if (!thread || !message || message.length > MAX_MESSAGE_CHARS) return;
      if (pendingItem(thread) || starting.current.has(thread.threadId)) return;
      const candidate = retryCandidate(thread, message);
      const request: OutgoingRequest = candidate?.request
        ? {
            requestId: candidate.request.requestId,
            message: candidate.content,
            sentConversationId: candidate.request.sentConversationId,
          }
        : { requestId: newUuid(), message, sentConversationId: thread.conversationId };
      start(thread.threadId, request);
    },
    [start],
  );

  const retry = useCallback(
    (requestId: string) => {
      const current = stateRef.current;
      const thread = current.threads[current.activeThreadId];
      if (!thread || starting.current.has(thread.threadId)) return;
      const item = thread.items.find(
        (candidate): candidate is UserItem => candidate.kind === "user" && candidate.request?.requestId === requestId,
      );
      if (!item?.request || !canRetryItem(thread, item)) return;
      start(thread.threadId, {
        requestId,
        message: item.content,
        sentConversationId: item.request.sentConversationId,
      });
    },
    [start],
  );

  const openConversation = useCallback(
    (conversationId: string) => {
      const current = stateRef.current;
      const existing = findThreadByConversation(current, conversationId);
      const threadId = existing?.threadId ?? `c:${conversationId}`;
      const title = current.conversations.find((c) => c.conversation_id === conversationId)?.title ?? null;
      dispatch({ type: "openConversation", threadId, conversationId, title });
      // A thread with a live request keeps its state; reloading could not show anything newer.
      if (existing && (pendingItem(existing) || starting.current.has(existing.threadId))) return;
      void loadThread(threadId, conversationId);
    },
    [loadThread],
  );

  const openThread = useCallback((threadId: string) => dispatch({ type: "selectThread", threadId }), []);

  const newConversation = useCallback(
    (draft?: string) => dispatch({ type: "newThread", threadId: newThreadId(), draft }),
    [],
  );

  const setDraft = useCallback((draft: string) => {
    dispatch({ type: "draftChanged", threadId: stateRef.current.activeThreadId, draft });
  }, []);

  const reloadThread = useCallback(() => {
    const thread = selectActiveThread(stateRef.current);
    if (thread.conversationId !== null && !pendingItem(thread)) void loadThread(thread.threadId, thread.conversationId);
  }, [loadThread]);

  useEffect(() => {
    void refreshConversations();
  }, [refreshConversations]);

  const thread = selectActiveThread(state);
  return useMemo(
    () => ({
      state,
      thread,
      send,
      retry,
      setDraft,
      openConversation,
      openThread,
      newConversation,
      reloadThread,
      refreshConversations: () => void refreshConversations(),
    }),
    [state, thread, send, retry, setDraft, openConversation, openThread, newConversation, reloadThread, refreshConversations],
  );
}
