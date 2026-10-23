import { useEffect, useRef, useState } from "react";
import { ticketId } from "../store/store";
import styles from "./Actions.module.css";
import { useView } from "./StoreContext";

/** Lines of the agent's pane asked for (the synthesis reads 180 too). */
export const PANE_LINES = 180;
/** How long after a pane arrives the next is asked for. */
export const PANE_MS = 4000;

/**
 * The agent's pane, live while mounted: asks for it (tk.sh show), then again
 * PANE_MS after each answer, or after a failed ask; an answer lost on the way
 * (a /ws/ui gap) is asked again after PANE_MS * 3.
 */
export function PaneTail({ boardKey, nn, poll }: {
  boardKey: string;
  nn: string;
  poll: (boardKey: string, nn: string, lines: number) => Promise<void>;
}) {
  const pane = useView().panes[ticketId(boardKey, nn)];
  const [retry, setRetry] = useState(0);
  const asked = useRef(false);
  useEffect(() => {
    let live = true;
    let watchdog: ReturnType<typeof setTimeout> | undefined;
    const ask = (): Promise<void> => {
      clearTimeout(watchdog);
      watchdog = setTimeout(() => live && void ask(), PANE_MS * 3);
      return poll(boardKey, nn, PANE_LINES).catch(() => {
        // Not sent (hub or box offline): ask again later.
        clearTimeout(watchdog);
        if (live) setTimeout(() => live && setRetry((n) => n + 1), PANE_MS);
      });
    };
    // The first ask goes at once (a pane kept from before may be old); each
    // answer (a new `pane`) or failed ask (`retry`) schedules the next.
    let timer: ReturnType<typeof setTimeout> | undefined;
    if (asked.current) timer = setTimeout(ask, PANE_MS);
    else {
      asked.current = true;
      void ask();
    }
    return () => {
      live = false;
      clearTimeout(timer);
      clearTimeout(watchdog);
    };
  }, [poll, boardKey, nn, pane, retry]);
  if (!pane) return <p className={styles.muted}>Loading the agent's pane...</p>;
  if (!pane.ok) return <p className={styles.muted}>{pane.reason}</p>;
  return <pre className={styles.pane}>{pane.text || "(empty)"}</pre>;
}
