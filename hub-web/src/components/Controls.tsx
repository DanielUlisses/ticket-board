import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { GROUPS, type Group } from "../columns";
import { isBoardShown, presetOf, stagesFor, type Stage } from "../store/store";
import type { Board } from "../protocol/types";
import styles from "./Controls.module.css";
import { useStore, useView } from "./StoreContext";

const PILLS: {
  stage: Stage;
  label: string;
  dot?: "urgent" | "working";
  counted: boolean;
}[] = [
  { stage: "all", label: "All", counted: false },
  { stage: "attention", label: "Needs You", dot: "urgent", counted: true },
  { stage: "active", label: "Active", dot: "working", counted: true },
  { stage: "frontier", label: "Frontier", counted: true },
];

/** A button showing a summary that opens a panel (Escape or a click outside closes it). */
function Dropdown({ label, summary, children }: { label: string; summary: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const panel = useId();
  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent | TouchEvent) => {
      if (!root.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", away);
    document.addEventListener("touchstart", away);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("touchstart", away);
    };
  }, [open]);
  return (
    <div
      className={styles.dropdown}
      ref={root}
      onBlur={(e) => {
        // A null relatedTarget is a click inside the panel in some browsers; outside clicks are handled above.
        const next = e.relatedTarget as Node | null;
        if (open && next && !root.current?.contains(next)) setOpen(false);
      }}
      onKeyDown={(e) => {
        if (e.key === "Escape" && open) {
          setOpen(false);
          button.current?.focus();
        }
      }}
    >
      <button
        type="button"
        ref={button}
        className={`${styles.select} ${styles.trigger}`}
        aria-label={label}
        aria-expanded={open}
        aria-controls={panel}
        onClick={() => setOpen(!open)}
      >
        {summary}
      </button>
      {open && (
        <div id={panel} role="group" aria-label={`${label} filter`} className={styles.panel}>
          {children}
        </div>
      )}
    </div>
  );
}

function AllNone({ what, onAll, onNone }: { what: string; onAll: () => void; onNone: () => void }) {
  return (
    <div className={styles.shortcuts}>
      <button type="button" className={styles.link} aria-label={`All ${what}`} onClick={onAll}>
        All
      </button>
      <button type="button" className={styles.link} aria-label={`No ${what}`} onClick={onNone}>
        None
      </button>
    </div>
  );
}

const boardName = (b: Board) => `${b.slug} · ${b.repo.slice(0, 18)}`;

/** Devbox select, Stage and Board multi-selects, and the stage pills (presets of Stage). */
export function Controls({
  onArchive,
}: {
  onArchive?: (boardKey: string, archived: boolean) => void;
}) {
  const store = useStore();
  const { filter, counts, devboxes, allBoards, archivedBoards } = useView();
  const boxNames = [
    ...new Set([
      ...devboxes.map((d) => d.devbox),
      ...allBoards.map((b) => b.devbox),
    ]),
  ];
  const inBox = (b: Board) => filter.devbox === null || b.devbox === filter.devbox;
  const live = allBoards.filter(inBox);
  const archived = archivedBoards.filter(inBox);
  const listed = [...live, ...archived];
  const shown = listed.filter((b) => isBoardShown(filter, b));
  const pick = (keys: string[], on: boolean) =>
    store.setFilter({
      boards: { ...filter.boards, ...Object.fromEntries(keys.map((k) => [k, on])) },
    });
  /** Archiving or restoring changes a board's default, so its pick is dropped. */
  const archive = (key: string, to: boolean) => {
    const { [key]: _pick, ...rest } = filter.boards;
    store.setFilter({ boards: rest });
    onArchive?.(key, to);
  };
  const toggleStage = (g: Group) =>
    store.setFilter({
      stages: GROUPS.map((x) => x.group).filter((x) =>
        x === g ? !filter.stages.includes(g) : filter.stages.includes(x),
      ),
    });
  const pressed = presetOf(filter.stages);
  const showBox = filter.devbox === null && new Set(listed.map((b) => b.devbox)).size > 1;
  const row = (b: Board, isArchived: boolean) => (
    <div key={b.key} className={styles.row} data-archived={isArchived}>
      <label>
        <input
          type="checkbox"
          checked={isBoardShown(filter, b)}
          onChange={(e) => pick([b.key], e.target.checked)}
        />
        <span className={styles.rowName}>
          {boardName(b)}
          {showBox && <span className={styles.box}> · {b.devbox}</span>}
          {isArchived && <span className={styles.tag}> archived</span>}
        </span>
      </label>
      <button
        type="button"
        className={styles.action}
        aria-label={`${isArchived ? "Restore" : "Archive"} ${boardName(b)}`}
        disabled={b.stale}
        onClick={() => archive(b.key, !isArchived)}
      >
        {isArchived ? "Restore" : "Archive"}
      </button>
    </div>
  );
  return (
    <div className={styles.bar}>
      <select
        className={styles.select}
        aria-label="Devbox"
        value={filter.devbox ?? ""}
        onChange={(e) => store.setFilter({ devbox: e.target.value || null })}
      >
        <option value="">All Devboxes ({boxNames.length})</option>
        {boxNames.map((name) => (
          <option key={name} value={name}>
            {name}
          </option>
        ))}
      </select>
      <Dropdown label="Board" summary={`Boards (${shown.length})`}>
        <AllNone
          what="boards"
          onAll={() => pick(live.map((b) => b.key), true)}
          onNone={() => pick(listed.map((b) => b.key), false)}
        />
        {live.map((b) => row(b, false))}
        {archived.length > 0 && (
          <>
            <div className={styles.heading}>Archived ({archived.length})</div>
            {archived.map((b) => row(b, true))}
          </>
        )}
      </Dropdown>
      <Dropdown label="Stage" summary={`Stages (${filter.stages.length})`}>
        <AllNone
          what="stages"
          onAll={() => store.setFilter({ stages: stagesFor("all") })}
          onNone={() => store.setFilter({ stages: [] })}
        />
        {GROUPS.map((g) => (
          <div key={g.group} className={styles.row}>
            <label>
              <input
                type="checkbox"
                checked={filter.stages.includes(g.group)}
                onChange={() => toggleStage(g.group)}
              />
              <span className={styles.rowName}>{g.label}</span>
            </label>
          </div>
        ))}
      </Dropdown>
      <span className={styles.pills}>
        {PILLS.map((p) => (
          <button
            key={p.stage}
            type="button"
            className={styles.pill}
            aria-pressed={pressed === p.stage}
            onClick={() => store.setFilter({ stages: stagesFor(p.stage) })}
          >
            {p.dot && (
              <span
                className={styles.dot}
                data-dot={p.dot}
                aria-hidden="true"
              />
            )}
            {p.counted ? `${p.label} (${counts[p.stage]})` : p.label}
          </button>
        ))}
      </span>
    </div>
  );
}
