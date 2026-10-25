import { useEffect, useLayoutEffect, useRef, useState, type RefObject } from "react";
import { canRetryItem, pendingItem, type Thread } from "../state/chatStore";
import { Composer } from "./Composer";
import { Message } from "./Message";

const EXAMPLE_QUESTIONS = [
  "Which airports in New England are strong candidates for terminal expansion?",
  "Compare LA and Santa Ana airport congestion levels.",
  "What is the percentage of long haul flights out of Anchorage airport?",
  "What is the unmet flight demand in SFO airport and why?",
] as const;

interface ChatProps {
  thread: Thread;
  onSend: (text: string) => void;
  onRetry: (requestId: string) => void;
  onDraftChange: (draft: string) => void;
  onReload: () => void;
  onStartNewConversation: (draft?: string) => void;
  onShowConversations: () => void;
  composerRef: RefObject<HTMLTextAreaElement>;
}

function EmptyState({ onPick }: { onPick: (question: string) => void }) {
  return (
    <section className="empty" aria-labelledby="empty-title">
      <h2 id="empty-title" className="empty__title">
        Ask about U.S. airport capacity
      </h2>
      <p className="empty__text">
        Rank expansion candidates, compare congestion, measure long-haul share or look for signs of unmet demand.
        Results are computed from FAA and BTS public data with fixed rules and show their periods, sources and
        limitations.
      </p>
      <p className="empty__label">Try one of these questions:</p>
      <ul className="suggestions">
        {EXAMPLE_QUESTIONS.map((question) => (
          <li key={question}>
            <button type="button" className="suggestion" onClick={() => onPick(question)}>
              {question}
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function Chat({
  thread,
  onSend,
  onRetry,
  onDraftChange,
  onReload,
  onStartNewConversation,
  onShowConversations,
  composerRef,
}: ChatProps) {
  const busy = pendingItem(thread) !== undefined;
  const scrollRef = useRef<HTMLDivElement>(null);
  const last = thread.items[thread.items.length - 1];
  const lastKey = last?.key;
  const lastKind = last?.kind;
  const shown = useRef<{ threadId: string; lastKey: string | undefined }>({ threadId: "", lastKey: undefined });
  const [announcement, setAnnouncement] = useState("");

  // Bring the newest message into view: the start of a new answer, or the bottom after a question.
  useLayoutEffect(() => {
    const container = scrollRef.current;
    const previous = shown.current;
    shown.current = { threadId: thread.threadId, lastKey };
    if (!container || (previous.threadId === thread.threadId && previous.lastKey === lastKey)) return;
    const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
    const behavior: ScrollBehavior = previous.threadId === thread.threadId && !reduceMotion ? "smooth" : "auto";
    // Scroll only this container (scrollIntoView would also scroll the page around it).
    const answer = lastKind === "assistant" && lastKey ? container.querySelector<HTMLElement>(`[data-key="${lastKey}"]`) : null;
    const top = answer
      ? answer.getBoundingClientRect().top - container.getBoundingClientRect().top + container.scrollTop - 12
      : container.scrollHeight;
    container.scrollTo({ top, behavior });
  }, [thread.threadId, lastKey, lastKind]);

  // Announce new answers to screen readers without reading the whole answer aloud.
  const answerKey = lastKind === "assistant" ? lastKey : undefined;
  const announcedThread = useRef(thread.threadId);
  useEffect(() => {
    if (announcedThread.current !== thread.threadId) {
      announcedThread.current = thread.threadId;
      return;
    }
    if (answerKey?.startsWith("a:")) setAnnouncement("A new answer was received.");
  }, [answerKey, thread.threadId]);

  // The server titles a conversation after its first message; use that until the title is loaded.
  const firstQuestion = thread.items.find((item) => item.kind === "user")?.content;
  const title = thread.title ?? firstQuestion ?? "New conversation";

  return (
    <main className="chat" aria-label="Chat">
      <div className="chat__titlebar">
        <button type="button" className="btn btn--ghost chat__conversations" onClick={onShowConversations}>
          Conversations
        </button>
        <h1 className="chat__title" dir="auto" title={title}>
          {title}
        </h1>
      </div>

      <div className="chat__scroll" ref={scrollRef}>
        <div className="chat__inner">
          {thread.load === "error" && (
            <div className="alert alert--error" role="alert">
              <span>{thread.loadError ?? "The conversation could not be loaded."}</span>{" "}
              <button type="button" className="btn btn--small" onClick={onReload}>
                Try again
              </button>
            </div>
          )}
          {thread.load === "loading" && thread.items.length === 0 && (
            <p className="chat__loading" role="status">
              Loading conversation…
            </p>
          )}
          {thread.load === "new" && thread.items.length === 0 && <EmptyState onPick={onSend} />}
          {thread.load === "loaded" && thread.items.length === 0 && (
            <p className="muted">This conversation has no messages yet.</p>
          )}
          {thread.items.length > 0 && (
            <ol className="messages" aria-label="Messages">
              {thread.items.map((item) => (
                <Message
                  key={item.key}
                  item={item}
                  canRetry={item.kind === "user" && canRetryItem(thread, item)}
                  onRetry={onRetry}
                  onStartNewConversation={onStartNewConversation}
                />
              ))}
            </ol>
          )}
        </div>
      </div>

      <p className="visually-hidden" aria-live="polite">
        {announcement}
      </p>
      <Composer
        value={thread.draft}
        onChange={onDraftChange}
        onSend={() => onSend(thread.draft)}
        busy={busy}
        textareaRef={composerRef}
      />
    </main>
  );
}
