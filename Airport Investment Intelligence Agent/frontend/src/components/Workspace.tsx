import { useCallback, useMemo, useRef, useState } from "react";
import type { SessionApi } from "../api/client";
import { sidebarEntries, type SidebarEntry } from "../state/chatStore";
import { useChatController } from "../state/useChatController";
import type { User } from "../types";
import { Chat } from "./Chat";
import { ConversationList } from "./ConversationList";
import { Header } from "./Header";
import { RefreshButton } from "./RefreshButton";

export interface ActiveSession {
  /** Increases with every sign-in; the workspace is keyed by it so no state survives a session. */
  epoch: number;
  user: User;
  controller: AbortController;
  /** Mutable holder so a refreshed CSRF token is used without rebuilding the API client. */
  csrf: { token: string };
  api: SessionApi;
  notice: string | null;
}

interface WorkspaceProps {
  session: ActiveSession;
  onLogout: () => void;
  onSessionCheck: () => void;
}

/** The signed-in screen. Mounted once per session, so unmounting it discards all private state. */
export function Workspace({ session, onLogout, onSessionCheck }: WorkspaceProps) {
  const chat = useChatController(session.api, session.controller.signal, onSessionCheck);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [notice, setNotice] = useState(session.notice);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const entries = useMemo(() => sidebarEntries(chat.state), [chat.state]);

  const focusComposer = () => window.requestAnimationFrame(() => composerRef.current?.focus());
  const closeSidebar = useCallback(() => setSidebarOpen(false), []);

  const select = (entry: SidebarEntry) => {
    if (entry.conversationId) chat.openConversation(entry.conversationId);
    else if (entry.threadId) chat.openThread(entry.threadId);
    setSidebarOpen(false);
  };

  const startNew = (draft?: string) => {
    chat.newConversation(draft);
    setSidebarOpen(false);
    focusComposer();
  };

  return (
    <div className="app">
      <Header
        username={session.user.username}
        onLogout={onLogout}
        refresh={<RefreshButton api={session.api} signal={session.controller.signal} />}
      />
      {notice && (
        <div className="banner" role="status">
          <span>{notice}</span>
          <button type="button" className="btn btn--ghost btn--small" onClick={() => setNotice(null)}>
            Dismiss
          </button>
        </div>
      )}
      <div className="layout">
        <ConversationList
          entries={entries}
          status={chat.state.conversationsStatus}
          error={chat.state.conversationsError}
          open={sidebarOpen}
          onClose={closeSidebar}
          onNew={() => startNew()}
          onSelect={select}
          onRetry={chat.refreshConversations}
        />
        <Chat
          thread={chat.thread}
          onSend={chat.send}
          onRetry={chat.retry}
          onDraftChange={chat.setDraft}
          onReload={chat.reloadThread}
          onStartNewConversation={startNew}
          onShowConversations={() => setSidebarOpen(true)}
          composerRef={composerRef}
        />
      </div>
    </div>
  );
}
