import { useEffect, useId, useState } from "react";
import type { HubClient, ShipMethod } from "../api/client";
import type { Board, Pr, RpcResult, Ticket } from "../protocol/types";
import styles from "./Actions.module.css";
import { Spinner } from "./Spinner";
import { ticketId } from "../store/store";
import { useView } from "./StoreContext";

export type ShipApi = Pick<HubClient, "ship" | "resolveNoCode" | "diff">;

/**
 * require_human_before_pr: why ⇡ Open PR / ⇡ Push via SSH are held, or null when
 * they are not (no flag, or an approval of the diff as it is now). The diff's
 * hash is fetched while the gate applies; the daemon refuses on its own as well.
 */
function useApprovalGate(board: Board, ticket: Ticket, api: ShipApi): string | null {
  const gated = ticket.col === "human" && board.policy.require_human_before_pr;
  const approved = ticket.human_review?.approved ? ticket.human_review.diff_hash : null;
  const [current, setCurrent] = useState<{ hash: string | null; error?: string } | null>(null);
  useEffect(() => {
    if (!gated) return;
    let live = true;
    setCurrent(null);
    api.diff(board.key, ticket.nn).then(
      (r) => live && setCurrent({ hash: r.diff_hash }),
      (e: Error) => live && setCurrent({ hash: null, error: e.message }),
    );
    return () => {
      live = false;
    };
  }, [gated, api, board.key, ticket.nn, approved]);
  if (!gated) return null;
  if (!approved) return "Needs your review: approve the diff in the Review tab first (require_human_before_pr).";
  if (!current) return "Checking the approval against the diff...";
  if (current.error) return `Cannot check the approval: ${current.error}`;
  if (current.hash !== approved) return "The diff changed since approval: review it again in the Review tab.";
  return null;
}

/** One merge gate (tk.sh run_gates): its name, pass/fail and what tk.sh said. */
export interface Gate {
  gate: string;
  status: "pass" | "fail" | "unknown";
  detail: string;
}

const GATES = ["pr", "ready", "mergeable", "checks", "review"];

/** The gates as the PR looks now, before any merge attempt (same rules as tk.sh run_gates). */
export function gatesFromPr(pr: Pr | null): Gate[] {
  if (!pr) return [{ gate: "pr", status: "fail", detail: "no open PR" }];
  const bad = pr.checks.filter((c) => c.s === "bad").map((c) => c.n);
  const running = pr.checks.filter((c) => c.s === "pending").map((c) => c.n);
  const g = (gate: string, ok: boolean, detail = ""): Gate => ({ gate, status: ok ? "pass" : "fail", detail });
  return [
    g("pr", true, `#${pr.number}`),
    g("ready", !pr.isDraft, pr.isDraft ? "draft" : ""),
    g("mergeable", pr.mergeable === "MERGEABLE", pr.mergeable === "MERGEABLE" ? "" : pr.mergeable.toLowerCase()),
    g(
      "checks",
      bad.length + running.length === 0,
      bad.length ? `failed: ${bad.join(", ")}` : running.length ? `still running: ${running.join(", ")}` : "",
    ),
    g("review", pr.review === "APPROVED" || pr.review === "", pr.review === "APPROVED" ? "" : pr.review),
  ];
}

/** The gates a merge attempt reported (rpc_result data.gates), in tk.sh's order; null when it sent none. */
export function gatesFromResult(r: RpcResult | undefined): Gate[] | null {
  const raw = r?.data?.gates;
  if (!Array.isArray(raw) || raw.length === 0) return null;
  const known = raw as Gate[];
  return [...known].sort((a, b) => GATES.indexOf(a.gate) - GATES.indexOf(b.gate));
}

function GateList({ gates }: { gates: Gate[] }) {
  return (
    <ul className={styles.gates} aria-label="Merge gates">
      {gates.map((g) => (
        <li key={g.gate} data-gate={g.gate} data-status={g.status}>
          {g.status === "pass" ? "✓" : "✗"} {g.gate}
          {g.detail ? ` — ${g.detail}` : ""}
        </li>
      ))}
    </ul>
  );
}

/** tk.sh:385 prints the open PR as JSON text; show #<number> when it parses so. */
function prLabel(raw: unknown): string {
  if (raw == null) return "unknown";
  const text = String(raw);
  try {
    const n = (JSON.parse(text) as { number?: unknown } | null)?.number;
    if (typeof n === "number") return `#${n}`;
  } catch {
    // not JSON: the raw text is the answer
  }
  return text;
}

/** Verify's answer: tk.sh why's LANDED verdict and the open PR. */
function Verified({ result }: { result: RpcResult }) {
  if (!result.ok) return <p className={styles.muted}>Verify failed: {result.reason || result.output_tail.trim()}</p>;
  const d = result.data ?? {};
  return (
    <p className={styles.muted} aria-label="Verify result">
      Landed: <strong>{String(d.landed ?? "unknown")}</strong> · Open PR: {prLabel(d.open_pr)}
    </p>
  );
}

/** Resolve without code: tk.sh resolve --no-code needs a one-line reason. */
function Resolve({ label, pending, onResolve }: {
  label: string;
  pending: boolean;
  onResolve: (reason: string) => Promise<boolean>;
}) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  if (!open)
    return (
      <button type="button" className={styles.secondary} onClick={() => setOpen(true)}>
        {label}
      </button>
    );
  return (
    <div className={styles.relay}>
      <label htmlFor={id} className={styles.label}>
        Reason (one line)
      </label>
      <input id={id} className={styles.textarea} value={reason} onChange={(e) => setReason(e.target.value)} />
      <div className={styles.row}>
        <button type="button" className={styles.secondary} onClick={() => setOpen(false)}>
          Cancel
        </button>
        <button
          type="button"
          className={styles.primary}
          disabled={pending || !reason.trim()}
          aria-busy={pending}
          onClick={async () => {
            if (await onResolve(reason)) {
              setOpen(false);
              setReason("");
            }
          }}
        >
          {pending && <Spinner />}Resolve
        </button>
      </div>
    </div>
  );
}

/**
 * The ship footer: human → ⇡ Open PR (forge github|azure) or ⇡ Push via SSH (forge
 * none: never a PR), held under require_human_before_pr until the current diff is approved; pr → ✓ Merge with its gates, Verify; done and landed → Resolve (plain);
 * needs, review, human, pr → Resolve without code. Null where it has nothing.
 */
export function ShipActions({ board, ticket, api }: { board: Board; ticket: Ticket; api: ShipApi }) {
  const { pending, ship } = useView();
  const held = useApprovalGate(board, ticket, api);
  const waiting = (method: string) =>
    pending.some((p) => p.method === method && p.board_key === board.key && p.nn === ticket.nn);
  const last = ship[ticketId(board.key, ticket.nn)];
  const send = (label: string, method: ShipMethod) => () => void api.ship(label, board.key, ticket.nn, method);
  const noCode = ["needs", "review", "human", "pr"].includes(ticket.col);
  const landed = ticket.col === "done" && ticket.landed.startsWith("yes");
  // Landed: tk.sh refuses --no-code (it has code), so a plain resolve, one click, no reason.
  const resolve = landed ? (
    <button
      type="button"
      className={styles.secondary}
      disabled={waiting("resolve_ticket")}
      aria-busy={waiting("resolve_ticket")}
      onClick={send("Resolve", "resolve_ticket")}
    >
      {waiting("resolve_ticket") && <Spinner />}Resolve
    </button>
  ) : noCode ? (
    <Resolve
      label="Resolve without code"
      pending={waiting("resolve_nocode")}
      onResolve={(reason) => api.resolveNoCode(board.key, ticket.nn, reason)}
    />
  ) : null;

  if (ticket.col === "human") {
    const none = board.policy.forge === "none";
    const method = none ? "ssh_push" : "open_pr";
    const label = none ? "⇡ Push via SSH" : "⇡ Open PR";
    return (
      <div className={styles.relay}>
        {held && (
          <p className={styles.muted} aria-label="Approval">
            {held}
          </p>
        )}
        <div className={styles.row}>
          {resolve}
          <button
            type="button"
            className={styles.primary}
            data-primary="true"
            disabled={waiting(method) || held !== null}
            aria-busy={waiting(method)}
            onClick={send(label, method)}
          >
            {waiting(method) && <Spinner />}
            {label}
          </button>
        </div>
      </div>
    );
  }
  if (ticket.col === "pr") {
    if (board.policy.forge === "none")
      return (
        resolve && (
          <div className={styles.relay}>
            <div className={styles.row}>{resolve}</div>
          </div>
        )
      );
    const gates = gatesFromResult(last?.method === "merge_pr" ? last : undefined) ?? gatesFromPr(ticket.pr);
    return (
      <div className={styles.relay}>
        <GateList gates={gates} />
        {last?.method === "verify_landed" && <Verified result={last} />}
        {last?.method === "merge_pr" && last.reason && last.data?.merged === true && (
          <p className={styles.muted} role="alert">
            {last.reason}
          </p>
        )}
        <div className={styles.row}>
          {resolve}
          <button
            type="button"
            className={styles.secondary}
            disabled={waiting("verify_landed")}
            aria-busy={waiting("verify_landed")}
            onClick={send("Verify", "verify_landed")}
          >
            {waiting("verify_landed") && <Spinner />}Verify
          </button>
          <button
            type="button"
            className={styles.primary}
            data-primary="true"
            disabled={waiting("merge_pr")}
            aria-busy={waiting("merge_pr")}
            onClick={send("✓ Merge", "merge_pr")}
          >
            {waiting("merge_pr") && <Spinner />}✓ Merge
          </button>
        </div>
      </div>
    );
  }
  if (!resolve) return null;
  return (
    <div className={styles.relay}>
      {last?.method === "verify_landed" && <Verified result={last} />}
      <div className={styles.row}>{resolve}</div>
    </div>
  );
}
