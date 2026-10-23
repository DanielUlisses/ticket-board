import { GROUPS } from "../columns";
import type { Stage } from "../store/store";
import styles from "./Controls.module.css";
import { useStore, useView } from "./StoreContext";

const PILLS: { stage: Stage; label: string; dot?: "urgent" | "working"; counted: boolean }[] = [
  { stage: "all", label: "All", counted: false },
  { stage: "attention", label: "Needs You", dot: "urgent", counted: true },
  { stage: "active", label: "Active", dot: "working", counted: true },
  { stage: "frontier", label: "Frontier", counted: true },
];

/** Devbox / Board / Stage selects and the stage pills (the same filter as Stage). */
export function Controls() {
  const store = useStore();
  const { filter, counts, devboxes, allBoards } = useView();
  const boxNames = [...new Set([...devboxes.map((d) => d.devbox), ...allBoards.map((b) => b.devbox)])];
  const boards = allBoards.filter((b) => filter.devbox === null || b.devbox === filter.devbox);
  return (
    <div className={styles.bar}>
      <select
        className={styles.select}
        aria-label="Devbox"
        value={filter.devbox ?? ""}
        onChange={(e) => store.setFilter({ devbox: e.target.value || null, board: null })}
      >
        <option value="">All Devboxes ({boxNames.length})</option>
        {boxNames.map((name) => (
          <option key={name} value={name}>
            {name}
          </option>
        ))}
      </select>
      <select
        className={styles.select}
        aria-label="Board"
        value={filter.board ?? ""}
        onChange={(e) => store.setFilter({ board: e.target.value || null })}
      >
        <option value="">All Boards ({boards.length})</option>
        {boards.map((b) => (
          <option key={b.key} value={b.key}>
            {b.slug} · {b.repo.slice(0, 18)}
          </option>
        ))}
      </select>
      <select
        className={styles.select}
        aria-label="Stage"
        value={filter.stage}
        onChange={(e) => store.setFilter({ stage: e.target.value as Stage })}
      >
        <option value="all">All Stages</option>
        {GROUPS.map((g) => (
          <option key={g.group} value={g.group}>
            {g.label}
          </option>
        ))}
      </select>
      <span className={styles.pills}>
        {PILLS.map((p) => (
          <button
            key={p.stage}
            type="button"
            className={styles.pill}
            aria-pressed={filter.stage === p.stage}
            onClick={() => store.setFilter({ stage: p.stage })}
          >
            {p.dot && <span className={styles.dot} data-dot={p.dot} aria-hidden="true" />}
            {p.counted ? `${p.label} (${counts[p.stage]})` : p.label}
          </button>
        ))}
      </span>
    </div>
  );
}
