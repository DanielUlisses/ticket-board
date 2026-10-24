import { useState } from "react";
import type { Board, RetroData, RetroMark, SweepData } from "../protocol/types";
import { Markdown } from "./Markdown";
import styles from "./RetroSweep.module.css";
import { Spinner } from "./Spinner";

/** The hub calls closing a board makes; each resolves null when the hub refused (it toasts why). */
export interface RetroApi {
  runRetro: (boardKey: string) => Promise<RetroData | null>;
  applyRetro: (boardKey: string, mark: RetroMark, ids: string[]) => Promise<RetroData | null>;
  sweepBoard: (boardKey: string) => Promise<SweepData | null>;
  proposeMemory: (boardKey: string, text: string) => Promise<boolean>;
}

interface Proposal {
  id: string;
  title: string;
  body: string;
  mark: string | null;
}

/** `### M1 title` / `### E2 title` blocks of proposals.md, with their `**Mark:**` line pulled out. */
export function parseProposals(md: string | null): Proposal[] {
  const out: Proposal[] = [];
  let fence = false;
  let cur: { id: string; title: string; lines: string[] } | null = null;
  const close = () => {
    if (!cur) return;
    let mark: string | null = null;
    const body = cur.lines
      .filter((l) => {
        const m = /^\*\*Mark:\*\*\s*(\S+)/.exec(l);
        if (m) mark = m[1];
        return !m;
      })
      .join("\n")
      .trim();
    out.push({ id: cur.id, title: cur.title, body, mark });
    cur = null;
  };
  for (const line of (md ?? "").split("\n")) {
    if (/^\s*(```|~~~)/.test(line)) fence = !fence;
    const h = fence ? null : /^### ([ME][0-9]+)\s+(.*)$/.exec(line);
    if (h) {
      close();
      cur = { id: h[1], title: h[2].trim(), lines: [] };
    } else if (!fence && /^##(?!#)/.test(line)) close();
    else cur?.lines.push(line);
  }
  close();
  return out;
}

const MARKS: [string, RetroMark][] = [
  ["Approve", "approve"],
  ["Reject", "reject"],
  ["Defer", "defer"],
];

/**
 * Closing a board: Retro (once every ticket is done) lists ticket-retro's proposals by id
 * to approve / reject / defer, M items offer "→ memory ticket" into the memory field; Sweep
 * (once a retro has results) removes this board's leftovers and says what it kept.
 */
export function RetroSweep({ board, allDone, api }: { board: Board; allDone: boolean; api: Partial<RetroApi> }) {
  const [retro, setRetro] = useState<RetroData | null>(null);
  const [sweep, setSweep] = useState<SweepData | null>(null);
  const [busy, setBusy] = useState<"retro" | "sweep" | "mark" | "memory" | null>(null);
  const [memory, setMemory] = useState("");
  const results = retro?.results_md ?? board.retro_results_md ?? null;
  const proposals = parseProposals(retro?.proposals_md ?? board.retro_proposals_md ?? null);

  const run = async <T,>(what: "retro" | "sweep" | "mark", call: () => Promise<T | null> | undefined, set: (v: T) => void) => {
    setBusy(what);
    try {
      const v = await call();
      if (v) set(v);
    } finally {
      setBusy(null);
    }
  };
  const mark = (m: RetroMark, id: string) =>
    run("mark", () => api.applyRetro?.(board.key, m, [id]), setRetro);
  const propose = async () => {
    setBusy("memory");
    try {
      if (await api.proposeMemory?.(board.key, memory.trim())) setMemory("");
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className={styles.wrap}>
      <div className={styles.row}>
        <h3 className={styles.title}>Close the board</h3>
        {api.runRetro && (
          <button
            type="button"
            className={styles.btn}
            disabled={!allDone || busy !== null}
            aria-busy={busy === "retro"}
            title={allDone ? "Run the retro on this board" : "Every ticket must be done first"}
            onClick={() => void run("retro", () => api.runRetro?.(board.key), setRetro)}
          >
            {busy === "retro" && <Spinner />}Retro
          </button>
        )}
        {api.sweepBoard && (
          <button
            type="button"
            className={styles.btn}
            disabled={!results?.trim() || busy !== null}
            aria-busy={busy === "sweep"}
            title={results?.trim() ? "Remove this board's finished worktrees and branches" : "Run the retro first"}
            onClick={() => void run("sweep", () => api.sweepBoard?.(board.key), setSweep)}
          >
            {busy === "sweep" && <Spinner />}Sweep
          </button>
        )}
      </div>
      {proposals.map((p) => (
        <section key={p.id} role="group" aria-label={p.id} className={styles.proposal}>
          <div className={styles.row}>
            <strong>{p.id}</strong>
            <span>{p.title}</span>
            {p.mark && <span className={styles.mark}>{p.mark}</span>}
          </div>
          {p.body && <Markdown source={p.body} compact />}
          <div className={styles.row}>
            {MARKS.map(([label, m]) => (
              <button key={m} type="button" className={styles.small} disabled={busy !== null} onClick={() => void mark(m, p.id)}>
                {label}
              </button>
            ))}
            {p.id.startsWith("M") && (
              <button
                type="button"
                className={styles.small}
                onClick={() => setMemory(p.body ? `${p.title}\n\n${p.body}` : p.title)}
              >
                → memory ticket
              </button>
            )}
          </div>
        </section>
      ))}
      {api.proposeMemory && (proposals.length > 0 || memory) && (
        <div className={styles.memory}>
          <textarea
            aria-label="Memory proposal"
            rows={3}
            value={memory}
            placeholder="A rule for project memory"
            onChange={(e) => setMemory(e.target.value)}
          />
          <button
            type="button"
            className={styles.small}
            disabled={!memory.trim() || busy !== null}
            onClick={() => void propose()}
          >
            Propose memory ticket
          </button>
        </div>
      )}
      {results?.trim() && (
        <>
          <h4 className={styles.sub}>retro-results.md</h4>
          <Markdown source={results} compact />
        </>
      )}
      {sweep && (
        <div aria-label="Sweep result">
          <h4 className={styles.sub}>Swept</h4>
          {sweep.removed.length === 0 ? (
            <p className={styles.note}>nothing removed</p>
          ) : (
            <ul>
              {sweep.removed.map((r) => (
                <li key={`${r.kind}:${r.ref}`}>
                  removed {r.kind} {r.ref}
                </li>
              ))}
            </ul>
          )}
          {sweep.skipped.length > 0 && (
            <ul>
              {sweep.skipped.map((r) => (
                <li key={`${r.kind}:${r.ref}`}>
                  skipped {r.kind} {r.ref} ({r.reason})
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
