import { useState } from "react";
import type { LaunchSettings } from "../api/client";
import { TICKET_TYPES, type Ticket, type TicketType } from "../protocol/types";
import styles from "./Actions.module.css";
import { Spinner } from "./Spinner";

const MODELS = ["opus", "sonnet", "haiku"];
const EFFORTS = ["low", "medium", "high", "xhigh", "max"];

/**
 * The launch type: the branch's prefix, else the title's first word when it
 * names a type ("Fix the …", "docs: …"), else feat (board.md, Scheduler). The
 * hub's ▶ Frontier picks it the same way (hub.py ticket_type).
 */
export function ticketType(t: Pick<Ticket, "branch" | "title">): TicketType {
  for (const text of [t.branch, t.title]) {
    const word = text.trim().toLowerCase().split(/[-/:\s]/, 1)[0] as TicketType;
    if (TICKET_TYPES.includes(word)) return word;
  }
  return "feat";
}

const withValue = (options: readonly string[], value: string) =>
  value && !options.includes(value) ? [value, ...options] : options;

function Select({ label, value, options, onChange }: {
  label: string;
  value: string;
  options: readonly string[];
  onChange: (v: string) => void;
}) {
  return (
    <label className={styles.field}>
      <span>{label}</span>
      <select aria-label={label} value={value} onChange={(e) => onChange(e.target.value)}>
        {/* No suggestion: the select shows what state holds (nothing) until one is picked. */}
        {!value && <option value="">choose…</option>}
        {withValue(options, value).map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    </label>
  );
}

/**
 * ▶ Launch with the ticket's suggested type/model/effort; the 🔒 lock unlocks
 * selects to override them first. Locking again goes back to the suggestions.
 */
export function LaunchControl({ ticket, pending, onLaunch }: {
  ticket: Ticket;
  pending: boolean;
  onLaunch: (s: LaunchSettings) => void;
}) {
  const suggested: LaunchSettings = { type: ticketType(ticket), model: ticket.smodel, effort: ticket.seffort };
  const [locked, setLocked] = useState(true);
  const [chosen, setChosen] = useState<LaunchSettings>(suggested);
  const settings = locked ? suggested : chosen;
  const ready = !!settings.model && !!settings.effort;
  const toggle = () => {
    setChosen(suggested);
    setLocked(!locked);
  };
  return (
    <div className={styles.launch}>
      <div className={styles.row}>
        <button
          type="button"
          className={styles.lock}
          aria-pressed={!locked}
          aria-label={locked ? "Unlock model, effort and type" : "Lock model, effort and type to the suggestions"}
          title={locked ? "Override the suggestions" : "Use the suggestions"}
          onClick={toggle}
        >
          {locked ? "🔒" : "🔓"}
        </button>
        {locked ? (
          <span className={styles.settings}>
            {ready ? `${settings.type} · ${settings.model} · ${settings.effort}` : "no suggested model/effort: unlock to choose"}
          </span>
        ) : (
          <span className={styles.selects}>
            <Select label="Type" value={chosen.type} options={TICKET_TYPES}
                    onChange={(v) => setChosen({ ...chosen, type: v as TicketType })} />
            <Select label="Model" value={chosen.model} options={MODELS}
                    onChange={(v) => setChosen({ ...chosen, model: v })} />
            <Select label="Effort" value={chosen.effort} options={EFFORTS}
                    onChange={(v) => setChosen({ ...chosen, effort: v })} />
          </span>
        )}
        <button
          type="button"
          className={styles.primary}
          data-primary="true"
          disabled={pending || !ready}
          aria-busy={pending}
          onClick={() => onLaunch(settings)}
        >
          {pending && <Spinner />}▶ Launch
        </button>
      </div>
    </div>
  );
}
