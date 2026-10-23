import { BoardBar, type OnFrontier } from "./BoardBar";
import { Card } from "./Card";
import styles from "./CardGrid.module.css";
import type { BriefApi } from "./BoardPanel";
import type { PolicyApi } from "./PolicyPopover";
import { useView } from "./StoreContext";

/** The Cards page: one section per board, a grid of its tickets' cards. */
export function CardGrid({ client, onFrontier }: { client?: PolicyApi & Partial<BriefApi>; onFrontier?: OnFrontier } = {}) {
  const { boards, devboxes } = useView();
  if (boards.length === 0) return <p className={styles.empty}>No tickets match these filters.</p>;
  return (
    <div className={styles.page}>
      {boards.map((board) => (
        <section
          key={board.key}
          className={styles.board}
          data-stale={board.stale ? "true" : "false"}
          data-archived={board.hub_meta.archived ? "true" : "false"}
          aria-label={`${board.slug} ${board.repo} on ${board.devbox}`}
        >
          {board.hub_meta.archived && <span className={styles.archived}>archived</span>}
          <BoardBar
            board={board}
            box={devboxes.find((d) => d.devbox === board.devbox)}
            api={client}
            onFrontier={onFrontier}
          />
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
