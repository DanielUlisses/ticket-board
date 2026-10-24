import { COLUMN_META } from "../columns";
import type { Ticket } from "../protocol/types";
import styles from "./Card.module.css";
import { checksMetric, modelTag } from "./checks";
import { Glyph } from "./Glyph";
import { conflictLabel } from "./MergeConflict";
import { useStore } from "./StoreContext";

export function Card({ ticket, boardKey }: { ticket: Ticket; boardKey: string }) {
  const store = useStore();
  const open = () => store.openTicket(boardKey, ticket.nn);
  const summary = ticket.summary ?? ticket.note;
  const tags = [COLUMN_META[ticket.col].label, conflictLabel(ticket.merge_conflict), modelTag(ticket)].filter(
    (t): t is string => t !== null,
  );
  const metric = checksMetric(ticket);
  return (
    <article className={styles.card} aria-label={`${ticket.nn} ${ticket.title}`}
      role="button"
      tabIndex={0}
      onClick={open}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          open();
        }
      }}
    >
      <div>
        <div className={styles.head}>
          <span className={styles.headLeft}>
            <Glyph col={ticket.col} />
            <span className={styles.title}>
              #{ticket.nn} {ticket.title}
            </span>
          </span>
          <span className={styles.open} aria-hidden="true">
            ↗
          </span>
        </div>
        {summary && <p className={styles.summary}>{summary}</p>}
      </div>
      <div className={styles.foot}>
        <span className={styles.tags}>
          {tags.map((tag, i) => (
            <span key={i} className={styles.tag} data-testid="tag">
              {tag}
            </span>
          ))}
        </span>
        {metric && <span className={styles.metric}>{metric}</span>}
      </div>
    </article>
  );
}
