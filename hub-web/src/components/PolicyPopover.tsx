import { useEffect, useRef, useState, type ReactNode } from "react";
import type { Board, Policy, PolicyEdit } from "../protocol/types";
import styles from "./PolicyPopover.module.css";

/** The one hub call the popover makes. */
export interface PolicyApi {
  setPolicy: (boardKey: string, values: PolicyEdit) => Promise<boolean>;
}

const FLAGS = ["require_human_before_pr", "auto_merge", "night_shift"] as const;
/** How long an edit waits for the snapshot that carries it before the popover shows the real value again. */
const PENDING_MS = 15_000;

type EditKey = keyof PolicyEdit;
type Pending = Partial<Record<EditKey, { value: unknown }>>;

interface RowProps {
  name: string;
  source: string;
  saving?: boolean;
  /** Set on board-sourced rows: drops the board override. */
  onInherit?: () => void;
  note?: string;
  children: ReactNode;
}

function PolicyRow({ name, source, saving, onInherit, note, children }: RowProps) {
  return (
    <div role="group" aria-label={name} className={styles.row}>
      <span className={styles.name}>{name}</span>
      <span className={styles.value}>{children}</span>
      <span className={styles.source} data-source={source}>
        {saving ? (
          "saving…"
        ) : (
          <>
            {source}
            {onInherit && (
              <button type="button" className={styles.inherit} aria-label={`inherit ${name}`} onClick={onInherit}>
                ↺ inherit
              </button>
            )}
          </>
        )}
      </span>
      {note && (
        <span className={styles.warn} role="note">
          {note}
        </span>
      )}
    </div>
  );
}

/** A board's resolved policy and the layer of each value; edits write the board override. */
export function PolicyPopover({ board, api, onClose }: { board: Board; api: PolicyApi; onClose: () => void }) {
  const { policy } = board;
  const [pending, setPending] = useState<Pending>({});
  const timers = useRef(new Map<EditKey, ReturnType<typeof setTimeout>>());
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);
  useEffect(() => {
    const t = timers.current;
    return () => t.forEach(clearTimeout);
  }, []);

  const source = (key: keyof Policy) => board.policy_sources?.[key] ?? "default";
  // An edit is done once the snapshot carries it: a set shows as the board's value, a removal as no board source.
  const reflected = (key: EditKey, value: unknown) =>
    value === null ? source(key) !== "board" : policy[key] === value && source(key) === "board";
  const live = (key: EditKey) => {
    const p = pending[key];
    return p && !reflected(key, p.value) ? p : undefined;
  };

  const settle = (key: EditKey) => {
    clearTimeout(timers.current.get(key));
    timers.current.delete(key);
    setPending((p) => {
      const { [key]: _gone, ...rest } = p;
      return rest;
    });
  };
  useEffect(() => {
    for (const key of Object.keys(pending) as EditKey[]) if (!live(key)) settle(key);
  });

  const edit = (values: PolicyEdit) => {
    const keys = Object.keys(values) as EditKey[];
    setPending((p) => ({ ...p, ...Object.fromEntries(keys.map((k) => [k, { value: values[k] }])) }));
    for (const key of keys) {
      clearTimeout(timers.current.get(key));
      timers.current.set(key, setTimeout(() => settle(key), PENDING_MS));
    }
    void api.setPolicy(board.key, values).then((ok) => ok || keys.forEach(settle));
  };
  const row = (key: EditKey) => ({
    source: source(key),
    saving: !!live(key),
    onInherit: source(key) === "board" ? () => edit({ [key]: null }) : undefined,
  });
  const shown = <K extends EditKey>(key: K): NonNullable<PolicyEdit[K]> => {
    const p = live(key);
    return (p && p.value !== null ? p.value : policy[key]) as NonNullable<PolicyEdit[K]>;
  };
  const noRemote = source("forge") === "no-remote";
  return (
    <div className={styles.pop} role="dialog" aria-label="Board policy">
      <PolicyRow name="autonomy" {...row("autonomy")}>
        <select
          value={shown("autonomy")}
          aria-label="autonomy"
          disabled={!!live("autonomy")}
          onChange={(e) => edit({ autonomy: e.target.value as Policy["autonomy"] })}
        >
          <option value="assist">assist</option>
          <option value="auto">auto</option>
        </select>
      </PolicyRow>
      <PolicyRow
        name="forge"
        {...row("forge")}
        note={noRemote ? "repo has no remote — forge is forced to none" : undefined}
      >
        <select
          value={shown("forge")}
          aria-label="forge"
          disabled={source("forge") === "env" || noRemote || !!live("forge")}
          title={source("forge") === "env" ? "TICKET_FORGE on the box wins over the policy" : undefined}
          onChange={(e) => edit({ forge: e.target.value as Policy["forge"] })}
        >
          <option value="github">github</option>
          <option value="azure">azure</option>
          <option value="none">none</option>
        </select>
      </PolicyRow>
      {FLAGS.map((flag) => (
        <PolicyRow key={flag} name={flag} {...row(flag)}>
          <input
            type="checkbox"
            aria-label={flag}
            checked={shown(flag)}
            disabled={!!live(flag)}
            onChange={(e) => edit({ [flag]: e.target.checked })}
          />
        </PolicyRow>
      ))}
      <p className={styles.section}>Box-level (read-only)</p>
      <PolicyRow name="max_parallel" source={source("max_parallel")}>{policy.max_parallel}</PolicyRow>
      <PolicyRow name="usage_gate_pct" source={source("usage_gate_pct")}>{policy.usage_gate_pct}%</PolicyRow>
      <p className={styles.note}>Edits apply to this board only.</p>
    </div>
  );
}
