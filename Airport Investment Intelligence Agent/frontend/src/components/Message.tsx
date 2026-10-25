import { useElapsedSeconds } from "../hooks/useElapsedSeconds";
import { formatElapsed } from "../lib/format";
import { isPendingPhase, type AssistantItem, type ChatItem, type UserItem, type UserRequestState } from "../state/chatStore";
import { ErrorBoundary } from "./ErrorBoundary";
import { Markdown } from "./Markdown";
import { ResultsTable } from "./ResultsTable";
import { Limitations } from "./results/common";
import { Sources } from "./Sources";

interface MessageProps {
  item: ChatItem;
  canRetry: boolean;
  onRetry: (requestId: string) => void;
  onStartNewConversation: (text: string) => void;
}

export function Message({ item, canRetry, onRetry, onStartNewConversation }: MessageProps) {
  return item.kind === "user" ? (
    <UserMessage item={item} canRetry={canRetry} onRetry={onRetry} onStartNewConversation={onStartNewConversation} />
  ) : (
    <AssistantMessage item={item} />
  );
}

function UserMessage({ item, canRetry, onRetry, onStartNewConversation }: MessageProps & { item: UserItem }) {
  const request = item.request;
  return (
    <li className="msg msg--user" data-key={item.key}>
      <span className="visually-hidden">You wrote:</span>
      <div className="msg__bubble" dir="auto">
        {item.content}
      </div>
      {request && (
        <RequestStatus
          request={request}
          canRetry={canRetry}
          onRetry={() => onRetry(request.requestId)}
          onStartNewConversation={() => onStartNewConversation(item.content)}
        />
      )}
    </li>
  );
}

function failureText(request: UserRequestState): string {
  if (request.error) {
    const wait = request.error.retryAfterSeconds;
    return wait ? `${request.error.message} Try again in ${wait} s.` : request.error.message;
  }
  if (request.phase === "interrupted") return "Processing was interrupted before an answer was saved.";
  if (request.phase === "failed") return "This request failed before an answer was saved.";
  return "This request could not be completed.";
}

function RequestStatus({
  request,
  canRetry,
  onRetry,
  onStartNewConversation,
}: {
  request: UserRequestState;
  canRetry: boolean;
  onRetry: () => void;
  onStartNewConversation: () => void;
}) {
  const elapsed = useElapsedSeconds(isPendingPhase(request.phase) ? request.startedAt : null);

  if (request.phase === "completed") return null;

  if (isPendingPhase(request.phase)) {
    const text =
      request.phase === "queued"
        ? "Another request in this conversation is still processing. This message will be sent when it finishes."
        : request.phase === "sending"
          ? "Analyzing your question. This can take up to two minutes."
          : "Checking for the saved answer.";
    return (
      <div className="req req--pending">
        <span className="spinner" aria-hidden="true" />
        <span role="status">{text}</span>
        {request.startedAt !== null && (
          <span className="req__elapsed" aria-hidden="true">
            {formatElapsed(elapsed)}
          </span>
        )}
      </div>
    );
  }

  return (
    <div className="req req--failed" role={request.error ? "alert" : undefined}>
      <p className="req__text">{failureText(request)}</p>
      {request.retry === "new_message" && <p className="req__hint">Send it again as a new message.</p>}
      {(canRetry || request.error?.code === "conversation_not_found") && (
        <div className="req__actions">
          {canRetry && (
            <button type="button" className="btn btn--small" onClick={onRetry}>
              Retry
            </button>
          )}
          {request.error?.code === "conversation_not_found" && (
            <button type="button" className="btn btn--small" onClick={onStartNewConversation}>
              Start a new conversation with this message
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function AssistantMessage({ item }: { item: AssistantItem }) {
  const response = item.response;
  return (
    <li className="msg msg--assistant" data-key={item.key}>
      <span className="visually-hidden">Agent answer:</span>
      <ErrorBoundary fallback={<p className="alert alert--error">This answer could not be displayed.</p>}>
        <div className="answer">
          <Markdown text={response?.answer ?? item.content} />
        </div>
        {response?.results.map((result, index) => (
          <ErrorBoundary
            key={index}
            fallback={<p className="alert alert--error">This result could not be displayed.</p>}
          >
            <ResultsTable result={result} />
          </ErrorBoundary>
        ))}
        {response && <Limitations items={response.limitations} title="Limitations" />}
        {response && <Sources sources={response.sources} />}
      </ErrorBoundary>
    </li>
  );
}
