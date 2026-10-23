import { COLUMN_META, formatSeen } from "../columns";
import { COLUMNS } from "../protocol/types";
import { ticketId } from "../store/store";
import { checksMetric } from "./checks";
import { DevboxPill } from "./DevboxPill";
import styles from "./LiveBoard.module.css";
import { useStore, useView } from "./StoreContext";

/** The prototype colours only these three header glyphs; the rest inherit. */
const HEADER_TINT = new Set(["needs", "human", "working"]);

/** The Live Board page: a swimlane per board across the eight columns. */
export function LiveBoard() {
  const store = useStore();
  const { boards, devboxes, justMoved } = useView();
  return (
    <div className={styles.wrap}>
      <div className={styles.toolbar}>
        <span className={styles.toolbarLeft}>
          <span className={styles.dot} aria-hidden="true" />
          <strong className={styles.toolbarTitle}>Live Stage Swimlanes</strong>
          <span className={styles.toolbarSub}>WSS stream from tk-daemon</span>
        </span>
      </div>
      <div className={styles.scroll}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th className={styles.boardHead}>BOARD / DEVBOX</th>
              {COLUMNS.map((c) => {
                const meta = COLUMN_META[c];
                return (
                  <th key={c}>
                    <span
                      className={styles.headGlyph}
                      data-variant={HEADER_TINT.has(c) ? meta.variant : undefined}
                    >
                      {meta.glyph}
                    </span>
                    {meta.label}
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {boards.map((board) => (
              <tr
                key={board.key}
                className={styles.lane}
                data-stale={board.stale ? "true" : "false"}
                aria-label={`${board.slug} ${board.repo} on ${board.devbox}`}
              >
                <th scope="row" className={styles.boardCell}>
                  <span className={styles.boardCellInner}>
                    <span className={styles.id}>{board.slug}</span>
                    <strong className={styles.boardTitle}>{board.repo}</strong>
                    <DevboxPill name={board.devbox} box={devboxes.find((d) => d.devbox === board.devbox)} plain />
                    <span className={styles.forge}>{board.forge}</span>
                    {board.stale && board.last_seen && (
                      <span className={styles.forge}>last seen {formatSeen(board.last_seen)}</span>
                    )}
                  </span>
                </th>
                {COLUMNS.map((c) => (
                  <td key={c}>
                    {board.tickets
                      .filter((t) => t.col === c)
                      .map((t) => {
                        const id = ticketId(board.key, t.nn);
                        const metric = checksMetric(t);
                        return (
                          <div
                            key={t.nn}
                            className={styles.puck}
                            title={`${t.nn} ${t.title}`}
                            data-moved={justMoved.has(id) ? "true" : "false"}
                            onAnimationEnd={() => store.clearMoved(id)}
                          >
                            <span className={styles.puckTop}>
                              <span>#{t.nn}</span>
                              <span className={styles.puckModel}>{t.model || t.smodel}</span>
                            </span>
                            <span className={styles.puckTitle}>{t.title}</span>
                            {metric && <span className={styles.puckSub}>{metric}</span>}
                          </div>
                        );
                      })}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
