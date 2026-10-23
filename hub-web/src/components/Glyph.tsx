import { COLUMN_META } from "../columns";
import type { Column } from "../protocol/types";
import styles from "./Glyph.module.css";

/** A column's status glyph: the only coloured thing on a card (board.md). */
export function Glyph({ col }: { col: Column }) {
  const meta = COLUMN_META[col];
  return (
    <span className={styles.glyph} data-variant={meta.variant} title={meta.label} aria-label={meta.label}>
      {meta.glyph}
    </span>
  );
}
