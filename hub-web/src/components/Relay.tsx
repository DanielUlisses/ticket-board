import { useEffect, useId, useState } from "react";
import styles from "./Actions.module.css";
import { useView } from "./StoreContext";
import { Spinner } from "./Spinner";

/**
 * Relay (tk.sh say): a message for the ticket's agent. Kept until the agent
 * got it, so a refusal (at a dialog, no agent) loses nothing.
 */
export function Relay({ primary, pending, onSend }: {
  primary: boolean;
  pending: boolean;
  onSend: (message: string) => Promise<string | null>;
}) {
  const { settled } = useView();
  const id = useId();
  const [text, setText] = useState("");
  const [sent, setSent] = useState<string | null>(null);
  const delivered = sent !== null && settled[sent] === true;
  useEffect(() => {
    if (!delivered) return;
    setText("");
    setSent(null);
  }, [delivered]);
  return (
    <div className={styles.relay}>
      <label htmlFor={id} className={styles.label}>
        Relay (tk.sh say)
      </label>
      <textarea
        id={id}
        className={styles.textarea}
        rows={3}
        value={text}
        placeholder="A message for the ticket's agent"
        onChange={(e) => setText(e.target.value)}
      />
      <div className={styles.row}>
        <button
          type="button"
          className={primary ? styles.primary : styles.secondary}
          data-primary={primary ? "true" : "false"}
          disabled={pending || !text.trim()}
          aria-busy={pending}
          onClick={async () => setSent(await onSend(text))}
        >
          {pending && <Spinner />}↵ Send
        </button>
      </div>
    </div>
  );
}
