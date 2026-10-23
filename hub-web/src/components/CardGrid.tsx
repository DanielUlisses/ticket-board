import { BoardBar } from "./BoardBar";
import { Card } from "./Card";
import styles from "./CardGrid.module.css";
import type { PolicyApi } from "./PolicyPopover";
import { useView } from "./StoreContext";

/** The Cards page: one section per board, a grid of its tickets' cards. */
export function CardGrid({ client }: { client?: PolicyApi }) {
  const { boards, devboxes } = useView();
  if (boards.length === 0) return <p className={styles.empty}>No tickets match these filters.</p>;
  return (
    <div className={styles.page}>
      {boards.map((board) => (
        <section
          key={board.key}
          className={styles.board}
          data-stale={board.stale ? "true" : "false"}
          aria-label={`${board.slug} ${board.repo} on ${board.devbox}`}
        >
          <BoardBar board={board} box={devboxes.find((d) => d.devbox === board.devbox)} api={client} />
          <div className={styles.grid}>
            {board.tickets.map((t) => (
              <Card key={t.nn} ticket={t} boardKey={board.key} />
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}
