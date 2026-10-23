import { useEffect, useRef, useState, type ReactNode } from "react";
import type { HubSettings, SchedulerEntry } from "../protocol/types";
import styles from "./Scheduler.module.css";
import { useView } from "./StoreContext";

/** The one hub call the scheduler controls make (`POST /api/settings`). */
export interface SchedulerApi {
  setSettings: (patch: Partial<HubSettings>) => Promise<boolean>;
}

const pad = (n: number) => String(n).padStart(2, "0");
/** Local HH:MM of a timestamp. */
const hhmm = (at: string | Date) => {
  const d = new Date(at);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
};
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
/** Local HH:MM, with the day and date in front when it isn't today ("Sat 24 Oct 06:00"). */
const startLabel = (d: Date) => {
  const now = new Date();
  const today =
    d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  return today ? hhmm(d) : `${DAYS[d.getDay()]} ${d.getDate()} ${MONTHS[d.getMonth()]} ${hhmm(d)}`;
};
/** A `datetime-local` value (local time) as a protocol timestamp (UTC, no milliseconds). */
const toUtc = (local: string) => new Date(local).toISOString().replace(/\.\d{3}Z$/, "Z");

/** A pill that opens a small panel; Escape or a click outside closes it. */
function Popover({ open, onClose, children }: { open: boolean; onClose: () => void; children: ReactNode }) {
  const root = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => root.current?.contains(e.target as Node) || onClose();
    const esc = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", esc);
    };
  }, [open, onClose]);
  return (
    <span className={styles.anchor} ref={root}>
      {children}
    </span>
  );
}

/**
 * ▶ Run waves: asks when to start (now, or at a chosen time), then the hub keeps launching
 * waves (plus PR/push/merge where policy allows) until nothing is left to launch, ship or
 * merge, or the usage gate holds every remaining launch, and turns it off. While on, a
 * click stops it.
 */
export function WavesButton({ api }: { api: SchedulerApi }) {
  const { settings } = useView();
  const [asking, setAsking] = useState(false);
  const [when, setWhen] = useState<"now" | "at">("now");
  const [at, setAt] = useState("");
  const on = settings?.waves ?? false;
  const start = settings?.waves_start ? new Date(settings.waves_start) : null;
  const label = !on
    ? "▶ Run waves"
    : start && start.getTime() > Date.now()
      ? `■ Waves at ${startLabel(start)}`
      : "■ Running waves";
  const click = () => {
    if (on) void api.setSettings({ waves: false });
    else setAsking((a) => !a);
  };
  const begin = () => {
    void api.setSettings({ waves: true, waves_start: when === "at" ? toUtc(at) : null });
    setAsking(false);
  };
  return (
    <Popover open={asking} onClose={() => setAsking(false)}>
      <button
        type="button"
        className={styles.pill}
        aria-pressed={on}
        aria-expanded={on ? undefined : asking}
        disabled={!settings}
        title={on ? "Stop launching waves" : "Launch frontier waves until none is left or the usage gate holds them"}
        onClick={click}
      >
        {label}
      </button>
      {asking && !on && (
        <div className={styles.pop} role="dialog" aria-label="Run waves">
          <p className={styles.section}>Start</p>
          <label className={styles.choice}>
            <input type="radio" name="waves-start" checked={when === "now"} onChange={() => setWhen("now")} />
            Now
          </label>
          <label className={styles.choice}>
            <input type="radio" name="waves-start" checked={when === "at"} onChange={() => setWhen("at")} />
            At
          </label>
          <input
            type="datetime-local"
            aria-label="Start at"
            className={styles.field}
            value={at}
            disabled={when !== "at"}
            onChange={(e) => setAt(e.target.value)}
          />
          <p className={styles.note}>
            Launches frontier waves on auto boards and on boards whose policy says night_shift, opens PRs, pushes and
            merges where their policy allows. Stops once nothing is left to launch, ship or merge, or the usage gate holds every launch.
          </p>
          <button type="button" className={styles.action} disabled={when === "at" && !at} onClick={begin}>
            Start
          </button>
        </div>
      )}
    </Popover>
  );
}

const ACTIONS: Record<SchedulerEntry["action"], string> = {
  sent: "sent",
  skipped: "skipped",
  failed: "failed",
  waves_off: "waves off",
};

function Decision({ entry }: { entry: SchedulerEntry }) {
  const what = [ACTIONS[entry.action], entry.method, entry.nn && `#${entry.nn}`].filter(Boolean).join(" ");
  const board = entry.board_key?.split("/").slice(1).join("/");
  return (
    <li className={styles.entry} data-action={entry.action}>
      <span className={styles.at}>{hhmm(entry.at)}</span> <span className={styles.what}>{what}</span>
      {board && <span className={styles.board}> {board}</span>}
      <span className={styles.reason}> — {entry.reason}</span>
    </li>
  );
}

/** The Scheduler panel: the total cap (a hub setting) and every decision, with its reason. */
export function SchedulerPanel({ api }: { api: SchedulerApi }) {
  const { settings, schedulerLog } = useView();
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<string | null>(null);
  const cap = Number(draft);
  const valid = draft !== null && Number.isInteger(cap) && cap >= 1 && cap <= 100;
  const save = () => {
    void api.setSettings({ total_cap: cap });
    setDraft(null);
  };
  return (
    <Popover open={open} onClose={() => setOpen(false)}>
      <button
        type="button"
        className={styles.pill}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        Scheduler
      </button>
      {open && (
        <div className={`${styles.pop} ${styles.wide}`} role="dialog" aria-label="Scheduler">
          <div className={styles.capRow}>
            <label htmlFor="total-cap" className={styles.section}>
              Total cap
            </label>
            <input
              id="total-cap"
              type="number"
              min={1}
              max={100}
              className={styles.field}
              value={draft ?? settings?.total_cap ?? ""}
              disabled={!settings}
              onChange={(e) => setDraft(e.target.value)}
            />
            <button type="button" className={styles.action} disabled={!valid} onClick={save}>
              Save
            </button>
          </div>
          <p className={styles.note}>Running tickets across every box; each box also keeps its max_parallel.</p>
          <p className={styles.section}>Decisions</p>
          {schedulerLog.length === 0 ? (
            <p className={styles.note}>No decisions yet.</p>
          ) : (
            <ol className={styles.log}>
              {schedulerLog.map((e) => (
                <Decision key={e.id} entry={e} />
              ))}
            </ol>
          )}
        </div>
      )}
    </Popover>
  );
}
