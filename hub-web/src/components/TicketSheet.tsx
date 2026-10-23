import { useCallback, useEffect, useRef, useState } from "react";
import type { HubClient } from "../api/client";
import { COLUMN_META } from "../columns";
import type { ActivityEvent, Board, Column, Ticket } from "../protocol/types";
import { Glyph } from "./Glyph";
import { LaunchControl } from "./LaunchControl";
import { PaneTail } from "./PaneTail";
import { Relay } from "./Relay";
import { ReportModal } from "./ReportModal";
import { useStore, useView } from "./StoreContext";
import styles from "./TicketSheet.module.css";

export { PANE_MS } from "./PaneTail";

/** The hub calls the sheet makes. */
export type DetailApi = Pick<HubClient, "activity" | "report" | "launch" | "say" | "paneTail" | "proposeMemory" | "refine" | "setTaskDone">;

/** Columns where the ticket has an agent: relay to it, tail its pane. */
const WITH_AGENT: ReadonlySet<Column> = new Set(["needs", "working", "review", "human"]);

/** The footer's actions by column: ▶ Launch on backlog, the relay (↵ Send, primary on needs) where there is an agent. */
function Actions({ board, ticket, api }: { board: Board; ticket: Ticket; api: DetailApi }) {
  const { pending } = useView();
  const waiting = (method: string) =>
    pending.some((p) => p.method === method && p.board_key === board.key && p.nn === ticket.nn);
  if (ticket.col === "backlog")
    return (
      <LaunchControl
        ticket={ticket}
        pending={waiting("launch_ticket")}
        onLaunch={(s) => void api.launch(board.key, ticket.nn, s)}
      />
    );
  if (WITH_AGENT.has(ticket.col))
    return (
      <Relay
        primary={ticket.col === "needs"}
        pending={waiting("say_agent")}
        onSend={(message) => api.say(board.key, ticket.nn, message)}
      />
    );
  return null;
}

/**
 * After ✦ Update, how long the spinner waits for the run to start (synthesis_started
 * sets the ticket running) before giving up: a refused request ends in a failed
 * rpc_result toast instead.
 */
export const REFINE_START_MS = 15000;

/**
 * How long a tick shows before the box confirms it (the ticket's next snapshot): a
 * tick the box failed (a failed rpc_result) or never ran goes back to the stored state.
 */
export const TICK_CONFIRM_MS = 15000;

/** `backlog → working 14:02`, then `→ review 15:40` for the moves after the first. */
function timelineText(e: ActivityEvent, first: boolean): string {
  return `${first ? `${e.from} ` : ""}→ ${e.to} ${e.at.slice(11, 16)}Z`;
}

/** The repo's project memory as list items (numbered or bulleted lines); read-only. */
function memoryItems(md: string | null): string[] {
  return (md ?? "")
    .split("\n")
    .map((l) => /^\s*(?:[-*]|\d+\.)\s+(.*\S)\s*$/.exec(l)?.[1])
    .filter((x): x is string => !!x);
}

/** "Propose to memory": prefilled from synthesis, editable; sends a ticket, never edits the file. */
function ProposeMemory({ board, ticket, api }: { board: Board; ticket: Ticket; api: DetailApi }) {
  const [typed, setTyped] = useState<string | null>(null); // null until the developer edits
  const text = typed ?? ticket.new_memory_rule ?? "";
  const setText = setTyped;
  const [busy, setBusy] = useState(false);
  const send = () => {
    setBusy(true);
    void api.proposeMemory(board.key, text.trim()).then((ok) => {
      setBusy(false);
      if (ok) setTyped(""); // sent: the prefill is spent too
    });
  };
  return (
    <div>
      <label className={styles.h} htmlFor="propose-memory">
        Propose to memory
      </label>
      <textarea
        id="propose-memory"
        rows={2}
        value={text}
        placeholder="A rule for docs/agents/project-memory.md"
        onChange={(e) => setText(e.target.value)}
      />
      <button type="button" className={styles.action} disabled={busy || !text.trim()} onClick={send}>
        Propose to memory
      </button>
    </div>
  );
}

function Timeline({ board, ticket, api }: { board: Board; ticket: Ticket; api: DetailApi }) {
  const [events, setEvents] = useState<ActivityEvent[] | null>(null);
  const [failed, setFailed] = useState(false);
  // A column change in the store means a new row in the log.
  useEffect(() => {
    let live = true;
    api.activity(board.key, ticket.nn).then(
      (e) => live && (setEvents(e), setFailed(false)),
      () => live && setFailed(true),
    );
    return () => {
      live = false;
    };
  }, [api, board.key, ticket.nn, ticket.col]);
  if (failed) return <p className={styles.muted}>Activity unavailable.</p>;
  if (!events) return <p className={styles.muted}>Loading...</p>;
  if (events.length === 0) return <p className={styles.muted}>No moves recorded.</p>;
  return (
    <ol className={styles.timeline}>
      {events.map((e, i) => (
        <li key={i}>{timelineText(e, i === 0)}</li>
      ))}
    </ol>
  );
}

/** ✦ Update via agy, with an optional comment; spins while a run is asked for or running. */
function Refine({ board, ticket, api }: { board: Board; ticket: Ticket; api: DetailApi }) {
  const [comment, setComment] = useState("");
  const [asked, setAsked] = useState(false);
  const status = ticket.synthesis_status;
  const seen = useRef(status);
  // Any status change (started, finished, failed) takes over from the request.
  useEffect(() => {
    if (seen.current === status) return;
    seen.current = status;
    setAsked(false);
  }, [status]);
  useEffect(() => {
    if (!asked) return;
    const timer = setTimeout(() => setAsked(false), REFINE_START_MS);
    return () => clearTimeout(timer);
  }, [asked]);

  async function send() {
    setAsked(true);
    if (await api.refine(board.key, ticket.nn, comment.trim())) setComment("");
    else setAsked(false);
  }

  const running = asked || status === "running";
  return (
    <div className={styles.refine}>
      <textarea
        className={styles.comment}
        aria-label="Comment for agy"
        placeholder="Optional: what the next summary should fix"
        rows={2}
        value={comment}
        onChange={(e) => setComment(e.target.value)}
      />
      <div className={styles.refineRow}>
        <button type="button" className={styles.action} disabled={running} onClick={() => void send()}>
          ✦ Update via agy
        </button>
        {running && (
          <span className={styles.running}>
            <span className={styles.spinner} role="status" aria-label="Synthesis running" />
            Updating...
          </span>
        )}
      </div>
    </div>
  );
}

/** The manual checklist: ticks go to the box's hub-meta and show at once, undone if the hub refuses or the box never confirms. */
function Checklist({ board, ticket, api }: { board: Board; ticket: Ticket; api: DetailApi }) {
  // Ticks not confirmed yet (text -> done), dropped once the ticket agrees.
  const [pending, setPending] = useState<Record<string, boolean>>({});
  const tasks = ticket.manual_tasks;
  useEffect(() => {
    setPending((p) => {
      const left = Object.entries(p).filter(([text, done]) => tasks.some((t) => t.text === text && t.done !== done));
      return left.length === Object.keys(p).length ? p : Object.fromEntries(left);
    });
  }, [tasks]);

  const timers = useRef(new Map<string, ReturnType<typeof setTimeout>>());
  useEffect(() => {
    const all = timers.current;
    return () => all.forEach(clearTimeout);
  }, []);

  function undo(text: string) {
    clearTimeout(timers.current.get(text));
    timers.current.delete(text);
    setPending((p) => {
      if (!(text in p)) return p;
      const { [text]: _undone, ...rest } = p;
      return rest;
    });
  }

  function tick(text: string, done: boolean) {
    setPending((p) => ({ ...p, [text]: done }));
    clearTimeout(timers.current.get(text));
    timers.current.set(text, setTimeout(() => undo(text), TICK_CONFIRM_MS));
    void api.setTaskDone(board.key, ticket.nn, text, done).then((ok) => {
      if (!ok) undo(text);
    });
  }

  if (tasks.length === 0) return <p className={styles.muted}>Nothing to validate by hand.</p>;
  return (
    <ul className={styles.list}>
      {tasks.map((t, i) => {
        const done = pending[t.text] ?? t.done;
        return (
          <li key={i} data-done={done ? "true" : "false"}>
            <label>
              <input type="checkbox" checked={done} onChange={(e) => tick(t.text, e.target.checked)} />
              <span>{t.text}</span>
            </label>
            {t.source === "developer" && (
              <span className={styles.tag} title="Added by you; synthesis never removes it">
                you
              </span>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function Sheet({ board, ticket, api }: { board: Board; ticket: Ticket; api: DetailApi }) {
  const store = useStore();
  const [report, setReport] = useState(false);
  const close = useCallback(() => store.closeTicket(), [store]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (report) setReport(false);
      else close();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [report, close]);

  const meta = COLUMN_META[ticket.col];
  const memory = memoryItems(board.project_memory);
  const loadReport = useCallback((k: string, nn: string) => api.report(k, nn), [api]);
  return (
    <div className={styles.backdrop} data-testid="sheet-backdrop" onClick={close}>
      <div
        className={styles.sheet}
        role="dialog"
        aria-modal="true"
        aria-label={`#${ticket.nn} ${ticket.title}`}
        onClick={(e) => e.stopPropagation()}
      >
        <header className={styles.head}>
          <Glyph col={ticket.col} />
          <div className={styles.headText}>
            <h2 className={styles.title}>
              #{ticket.nn} {ticket.title}
            </h2>
            <div className={styles.pills}>
              <span className={styles.pill}>{meta.label}</span>
              <span className={styles.pill}>{board.devbox}</span>
              <span className={styles.pill}>{board.slug}</span>
            </div>
          </div>
          <button type="button" className={styles.close} aria-label="Close" onClick={close}>
            ✕
          </button>
        </header>

        <div className={styles.scroll}>
          <section className={styles.card} aria-label="Synthesis">
            <h3 className={styles.h}>Synthesis</h3>
            {ticket.summary ? (
              <p className={styles.summary}>{ticket.summary}</p>
            ) : (
              <p className={styles.muted}>No summary yet.</p>
            )}
            {ticket.reviewer_notes && <p className={styles.notes}>{ticket.reviewer_notes}</p>}
            <dl className={styles.facts}>
              {ticket.branch && (
                <>
                  <dt>Branch</dt>
                  <dd className={styles.mono}>{ticket.branch}</dd>
                </>
              )}
              <dt>Synthesis</dt>
              <dd>{ticket.synthesis_backend ? `${ticket.synthesis_backend} · ${ticket.synthesis_status}` : ticket.synthesis_status}</dd>
            </dl>
            {ticket.synthesis_status === "failed" && ticket.synthesis_error && (
              <p className={styles.error}>{ticket.synthesis_error}</p>
            )}
            <Refine board={board} ticket={ticket} api={api} />
            <button type="button" className={styles.action} onClick={() => setReport(true)}>
              ⤢ Full Review &amp; Reasoning
            </button>
          </section>

          <section className={styles.card} aria-label="Manual Validation Checklist">
            <h3 className={styles.h}>Manual Validation Checklist</h3>
            <Checklist board={board} ticket={ticket} api={api} />
          </section>

          <section className={styles.card} aria-label="Project Memory">
            <h3 className={styles.h}>Project Memory</h3>
            {memory.length === 0 ? (
              <p className={styles.muted}>No project memory.</p>
            ) : (
              <ul className={styles.list}>
                {memory.map((m, i) => (
                  <li key={i}>{m}</li>
                ))}
              </ul>
            )}
            {ticket.new_memory_rule && (
              <p className={styles.suggestion} aria-label="Suggested memory rule">
                <span className={styles.tag}>Suggested</span> {ticket.new_memory_rule}
              </p>
            )}
            <ProposeMemory board={board} ticket={ticket} api={api} />
          </section>

          <section className={styles.card} aria-label="Activity">
            <h3 className={styles.h}>Activity</h3>
            <Timeline board={board} ticket={ticket} api={api} />
          </section>

          <section className={styles.card} aria-label="Agent pane">
            <h3 className={styles.h}>Agent pane</h3>
            {WITH_AGENT.has(ticket.col) ? (
              <PaneTail boardKey={board.key} nn={ticket.nn} poll={api.paneTail} />
            ) : (
              <p className={styles.muted}>No agent running.</p>
            )}
          </section>
        </div>

        <footer className={styles.foot} aria-label="Actions">
          <Actions board={board} ticket={ticket} api={api} />
        </footer>
      </div>
      {report && (
        <ReportModal boardKey={board.key} nn={ticket.nn} load={loadReport} onClose={() => setReport(false)} />
      )}
    </div>
  );
}

/** The ticket detail sheet: open while the store has a ticket selected, live with the store. */
export function TicketSheet({ client }: { client: DetailApi }) {
  const { sheet } = useView();
  if (!sheet) return null;
  return <Sheet key={`${sheet.board.key}#${sheet.ticket.nn}`} board={sheet.board} ticket={sheet.ticket} api={client} />;
}
