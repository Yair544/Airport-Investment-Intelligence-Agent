import { useId, useLayoutEffect, type KeyboardEvent, type RefObject } from "react";
import { formatInteger } from "../lib/format";
import { MAX_MESSAGE_CHARS } from "../state/useChatController";

interface ComposerProps {
  value: string;
  onChange: (value: string) => void;
  onSend: () => void;
  /** A request in this conversation is pending: typing stays possible, sending does not. */
  busy: boolean;
  textareaRef: RefObject<HTMLTextAreaElement>;
}

const MAX_HEIGHT_PX = 200;

export function Composer({ value, onChange, onSend, busy, textareaRef }: ComposerProps) {
  const id = useId();
  const length = value.trim().length;
  const tooLong = length > MAX_MESSAGE_CHARS;
  const canSend = !busy && length > 0 && !tooLong;

  useLayoutEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, MAX_HEIGHT_PX)}px`;
    textarea.style.overflowY = textarea.scrollHeight > MAX_HEIGHT_PX ? "auto" : "hidden";
  }, [value, textareaRef]);

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    if (canSend) onSend();
  };

  let hint = "Enter to send · Shift+Enter for a new line";
  if (busy) hint = "Waiting for the current answer. You can keep typing.";
  if (tooLong) {
    hint = `${formatInteger(length)} of ${formatInteger(MAX_MESSAGE_CHARS)} characters. Shorten the message to send it.`;
  }

  return (
    <form
      className="composer"
      onSubmit={(event) => {
        event.preventDefault();
        if (canSend) onSend();
      }}
    >
      <label htmlFor={id} className="visually-hidden">
        Message
      </label>
      <textarea
        id={id}
        ref={textareaRef}
        className="composer__input"
        value={value}
        rows={1}
        dir="auto"
        placeholder="Ask a question about U.S. airports…"
        aria-describedby={`${id}-hint`}
        aria-invalid={tooLong || undefined}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={onKeyDown}
      />
      <div className="composer__bar">
        <p id={`${id}-hint`} className={tooLong ? "composer__hint composer__hint--error" : "composer__hint"}>
          {hint}
        </p>
        <button type="submit" className="btn btn--primary" disabled={!canSend}>
          {busy ? "Waiting…" : "Send"}
        </button>
      </div>
    </form>
  );
}
