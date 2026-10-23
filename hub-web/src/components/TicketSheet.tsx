import { useCallback, useEffect, useState } from "react";
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
export type DetailApi = Pick<HubClient, "activity" | "report" | "launch" | "say" | "paneTail" | "proposeMemory">;

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
            <button type="button" className={styles.action} onClick={() => setReport(true)}>
              ⤢ Full Review &amp; Reasoning
            </button>
          </section>

          <section className={styles.card} aria-label="Manual Validation Checklist">
            <h3 className={styles.h}>Manual Validation Checklist</h3>
            {ticket.manual_tasks.length === 0 ? (
              <p className={styles.muted}>Nothing to validate by hand.</p>
            ) : (
              <ul className={styles.list}>
                {ticket.manual_tasks.map((t, i) => (
                  <li key={i} data-done={t.done ? "true" : "false"}>
                    <label>
                      <input type="checkbox" checked={t.done} readOnly disabled />
                      <span>{t.text}</span>
                    </label>
                  </li>
                ))}
              </ul>
            )}
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
