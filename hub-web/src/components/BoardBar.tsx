import { useState } from "react";
import { formatSeen } from "../columns";
import type { Board, Devbox } from "../protocol/types";
import styles from "./BoardBar.module.css";
import { DevboxPill } from "./DevboxPill";
import { Spinner } from "./Spinner";
import { useView } from "./StoreContext";
import { PolicyPopover, type PolicyApi } from "./PolicyPopover";

/** Starts a board's ▶ Frontier; the button is busy until the returned promise settles. */
export type OnFrontier = (boardKey: string) => Promise<unknown> | void;

/** ▶ Frontier: launch every backlog ticket of the board, within the box's caps. */
function FrontierButton({ boardKey, onFrontier }: { boardKey: string; onFrontier: OnFrontier }) {
  const { allBoards, pending } = useView();
  const [posting, setPosting] = useState(false);
  // Counted on the unfiltered board: a stage filter may hide its backlog.
  const ready = allBoards.find((b) => b.key === boardKey)?.tickets.filter((t) => t.col === "backlog").length ?? 0;
  const busy = posting || pending.some((p) => p.method === "launch_ticket" && p.board_key === boardKey);
  const click = async () => {
    setPosting(true);
    try {
      await onFrontier(boardKey);
    } finally {
      setPosting(false);
    }
  };
  return (
    <button
      type="button"
      className={styles.frontier}
      disabled={busy || ready === 0}
      aria-busy={busy}
      title="Launch this board's frontier within the box's caps"
      onClick={() => void click()}
    >
      {busy && <Spinner />}▶ Frontier ({ready})
    </button>
  );
}

/**
 * A board's bar: id pill, title (the repo), devbox, an autonomy · forge tag, staleness,
 * ▶ Frontier and, with an api, the policy settings popover.
 */
export function BoardBar({ board, box, api, onFrontier }: {
  board: Board;
  box: Devbox | undefined;
  api?: PolicyApi;
  onFrontier?: OnFrontier;
}) {
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
        {onFrontier && <FrontierButton boardKey={board.key} onFrontier={onFrontier} />}
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
