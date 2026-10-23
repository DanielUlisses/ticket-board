import { formatSeen } from "../columns";
import type { Board, Devbox } from "../protocol/types";
import styles from "./BoardBar.module.css";
import { DevboxPill } from "./DevboxPill";

/** A board's bar: id pill, title (the repo), devbox and forge sub-pills, staleness. */
export function BoardBar({ board, box }: { board: Board; box: Devbox | undefined }) {
  return (
    <header className={styles.bar}>
      <span className={styles.meta}>
        <span className={styles.id}>{board.slug}</span>
        <h2 className={styles.title}>{board.repo}</h2>
        <DevboxPill name={board.devbox} box={box} />
        <span className={styles.sub} title="forge">
          {board.forge}
        </span>
      </span>
      {board.stale && (
        <span className={styles.stale}>
          {board.last_seen ? `stale · last seen ${formatSeen(board.last_seen)}` : "stale"}
        </span>
      )}
    </header>
  );
}
