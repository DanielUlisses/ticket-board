import { useState } from "react";
import { formatSeen } from "../columns";
import type { Board, Devbox } from "../protocol/types";
import styles from "./BoardBar.module.css";
import { DevboxPill } from "./DevboxPill";
import { PolicyPopover, type PolicyApi } from "./PolicyPopover";

/**
 * A board's bar: id pill, title (the repo), devbox, an autonomy · forge tag, staleness
 * and, with an api, the policy settings popover.
 */
export function BoardBar({ board, box, api }: { board: Board; box: Devbox | undefined; api?: PolicyApi }) {
  const [open, setOpen] = useState(false);
  return (
    <header className={styles.bar}>
      <span className={styles.meta}>
        <span className={styles.id}>{board.slug}</span>
        <h2 className={styles.title}>{board.repo}</h2>
        <DevboxPill name={board.devbox} box={box} />
        <span className={styles.sub} title="autonomy · forge">
          {board.policy.autonomy} · {board.policy.forge}
        </span>
      </span>
      <span className={styles.end}>
        {board.stale && (
          <span className={styles.stale}>
            {board.last_seen ? `stale · last seen ${formatSeen(board.last_seen)}` : "stale"}
          </span>
        )}
        {api && (
          <span className={styles.settings}>
            <button
              type="button"
              className={styles.gear}
              aria-label="Board policy"
              aria-expanded={open}
              onClick={() => setOpen((o) => !o)}
            >
              ⚙
            </button>
            {open && <PolicyPopover board={board} api={api} onClose={() => setOpen(false)} />}
          </span>
        )}
      </span>
    </header>
  );
}
