import { useEffect, useRef } from "react";
import { formatShortDateTime } from "../lib/format";
import type { SidebarEntry } from "../state/chatStore";

interface ConversationListProps {
  entries: SidebarEntry[];
  status: "loading" | "loaded" | "error";
  error: string | null;
  /** Drawer state on narrow screens; the list is always visible on wide screens. */
  open: boolean;
  onClose: () => void;
  onNew: () => void;
  onSelect: (entry: SidebarEntry) => void;
  onRetry: () => void;
}

export function ConversationList({ entries, status, error, open, onClose, onNew, onSelect, onRetry }: ConversationListProps) {
  const newButton = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return undefined;
    newButton.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  return (
    <>
      {open && <div className="sidebar-backdrop" onClick={onClose} aria-hidden="true" />}
      <nav id="conversations" className={open ? "sidebar sidebar--open" : "sidebar"} aria-label="Conversations">
        <div className="sidebar__top">
          <button ref={newButton} type="button" className="btn btn--primary btn--block" onClick={onNew}>
            New conversation
          </button>
        </div>
        <div className="sidebar__list">
          {status === "error" && (
            <div className="alert alert--error sidebar__alert" role="alert">
              <span>{error ?? "Could not load conversations."}</span>{" "}
              <button type="button" className="btn btn--small" onClick={onRetry}>
                Try again
              </button>
            </div>
          )}
          {status === "loading" && entries.length === 0 && <p className="sidebar__empty">Loading conversations…</p>}
          {status === "loaded" && entries.length === 0 && <p className="sidebar__empty">No saved conversations yet.</p>}
          <ul className="convlist">
            {entries.map((entry) => (
              <li key={entry.key}>
                <button
                  type="button"
                  className="convlist__item"
                  aria-current={entry.active ? "true" : undefined}
                  onClick={() => onSelect(entry)}
                >
                  <span className="convlist__title" dir="auto">
                    {entry.title}
                  </span>
                  <span className="convlist__meta">
                    {entry.pending ? (
                      <span className="convlist__pending">Waiting for answer…</span>
                    ) : entry.updatedAt ? (
                      formatShortDateTime(entry.updatedAt)
                    ) : (
                      "Not saved yet"
                    )}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      </nav>
    </>
  );
}
