import { useEffect } from "react";
import { formatSeen } from "../columns";
import type { Board, BoardBrief } from "../protocol/types";
import styles from "./BoardPanel.module.css";
import { Markdown } from "./Markdown";
import { RetroSweep, type RetroApi } from "./RetroSweep";
import { Spinner } from "./Spinner";
import { useView } from "./StoreContext";

/** The one hub call the panel makes: a board-brief run now. */
export interface BriefApi {
  refreshBrief: (boardKey: string) => Promise<boolean>;
}

const SECTIONS = [
  ["Landed", "landed"],
  ["In flight", "in_flight"],
  ["Needs you", "needs_you"],
  ["Next", "next"],
] as const;

/** Which backend wrote the brief and how the last run went; a failed run keeps the lines above it. */
function BriefStatus({ brief }: { brief: BoardBrief }) {
  const by = brief.synthesis_backend ? ` by ${brief.synthesis_backend}` : "";
  const when = brief.updated_at ? ` · ${formatSeen(brief.updated_at)}` : "";
  const text =
    brief.synthesis_status === "running"
      ? `running${by}…`
      : brief.synthesis_status === "failed"
        ? `synthesis failed${brief.synthesis_backend ? `, showing the brief${by}${when}` : ""}`
        : brief.synthesis_status === "ok"
          ? `${by.trim()}${when}`
          : "not run yet";
  return (
    <span className={styles.status} data-status={brief.synthesis_status} data-testid="brief-status">
      {text}
    </span>
  );
}

/**
 * 📄 Board: why the board exists (board.md, sanitized markdown, or a note when the board has
 * none) and where it stands (the agy board brief: landed / in flight / needs you / next).
 * agy never writes board.md.
 */
export function BoardPanel({ board, api, onClose }: { board: Board; api?: Partial<BriefApi & RetroApi>; onClose: () => void }) {
  const { pending, allBoards } = useView();
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);
  // Judged on the unfiltered board: a stage filter may hide tickets that are not done.
  const all = allBoards.find((b) => b.key === board.key) ?? board;
  const allDone = all.tickets.length > 0 && all.tickets.every((t) => t.col === "done");
  const brief = board.hub_meta.board_brief ?? null;
  const hasLines = brief !== null && SECTIONS.some(([, k]) => brief[k].length > 0);
  const busy =
    brief?.synthesis_status === "running" ||
    pending.some((p) => p.method === "refresh_board_brief" && p.board_key === board.key);
  return (
    <div className={styles.backdrop} onClick={onClose}>
      <div className={styles.modal} role="dialog" aria-label="Board" onClick={(e) => e.stopPropagation()}>
        <div className={styles.head}>
          <strong>
            {board.slug} · {board.repo}
          </strong>
          <button type="button" className={styles.close} aria-label="Close" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className={styles.body}>
          <h3 className={styles.title}>Board doc</h3>
          {board.board_md && board.board_md.trim() ? (
            <Markdown source={board.board_md} />
          ) : (
            <p className={styles.note}>No board doc: this board has no board.md yet.</p>
          )}
          <div className={styles.briefHead}>
            <h3 className={styles.title}>Board brief</h3>
            {brief && <BriefStatus brief={brief} />}
            {api?.refreshBrief && (
              <button
                type="button"
                className={styles.refresh}
                disabled={busy}
                aria-busy={busy}
                onClick={() => void api.refreshBrief?.(board.key)}
              >
                {busy && <Spinner />}↻ Refresh
              </button>
            )}
          </div>
          {brief?.synthesis_status === "failed" && brief.synthesis_error && (
            <p className={styles.error}>{brief.synthesis_error}</p>
          )}
          {hasLines ? (
            <div className={styles.grid}>
              {SECTIONS.map(([label, key]) => (
                <section key={key} role="group" aria-label={label} className={styles.section}>
                  <h4>{label}</h4>
                  {brief[key].length === 0 ? (
                    <p className={styles.none}>nothing</p>
                  ) : (
                    <ul>
                      {brief[key].map((line, i) => (
                        <li key={`${i}:${line}`}>{line}</li>
                      ))}
                    </ul>
                  )}
                </section>
              ))}
            </div>
          ) : (
            <p className={styles.note}>No brief yet: ↻ Refresh asks agy for one.</p>
          )}
          {api && <RetroSweep board={board} allDone={allDone} api={api} />}
        </div>
      </div>
    </div>
  );
}
