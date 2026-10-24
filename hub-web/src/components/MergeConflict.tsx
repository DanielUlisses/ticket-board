import { useState } from "react";
import type { HubClient } from "../api/client";
import type { Board, MergeConflict, Ticket } from "../protocol/types";
import actions from "./Actions.module.css";
import { Markdown } from "./Markdown";
import styles from "./MergeConflict.module.css";
import { ReviewTab } from "./ReviewTab";
import { Spinner } from "./Spinner";
import sheet from "./TicketSheet.module.css";

export type ConflictApi = Pick<HubClient, "finishConflict" | "abortConflict">;

/** The card's tag for a conflict in flight or waiting for the developer; null otherwise. */
export function conflictLabel(mc: MergeConflict | null | undefined): string | null {
  switch (mc?.state) {
    case "resolving":
      return "resolving conflict";
    case "review":
      return "merge resolved — review";
    case "failed":
      return "conflict — resolver failed";
    default:
      return null;
  }
}

const DONE: Partial<Record<MergeConflict["state"], string>> = {
  committed: "Merge committed and pushed. The PR's checks re-run; ✓ Merge runs the gates again.",
  aborted: "Merge aborted.",
  up_to_date: "The branch already had the base.",
};

/**
 * The sheet's merge-conflict section: ticket-merger at work; its summary, the
 * conflicted files and the merge's staged diff against the branch's previous head
 * (the Review tab's view) for the developer's review; or why it failed.
 */
export function MergeConflictSection({ board, ticket, load }: {
  board: Board;
  ticket: Ticket;
  load: HubClient["diff"];
}) {
  const mc = ticket.merge_conflict;
  if (!mc) return null;
  const base = mc.base ?? "the base";
  const files = mc.files ?? [];
  return (
    <section className={sheet.card} aria-label="Merge conflict">
      <h3 className={sheet.h}>Merge conflict</h3>
      {mc.state === "resolving" && (
        <p className={styles.state} data-state="resolving" role="status">
          <Spinner />
          ticket-merger is resolving the conflict with {base}...
        </p>
      )}
      {mc.state === "review" && (
        <p className={styles.state} data-state="review">
          Merge resolved — review
        </p>
      )}
      {mc.state === "failed" && (
        <p className={styles.state} data-state="failed">
          Conflict — resolver failed
        </p>
      )}
      {DONE[mc.state] && <p className={sheet.muted}>{DONE[mc.state]}</p>}
      {mc.reason && <p className={mc.state === "failed" ? sheet.error : sheet.muted}>{mc.reason}</p>}
      {files.length > 0 && (
        <ul className={styles.files} aria-label="Conflicted files">
          {files.map((f) => (
            <li key={f}>{f}</li>
          ))}
        </ul>
      )}
      {mc.state === "review" && mc.summary && (
        <div className={sheet.summary}>
          <Markdown compact source={mc.summary} />
        </div>
      )}
      {mc.state === "failed" && mc.output_tail && <pre className={styles.tail}>{mc.output_tail}</pre>}
      {mc.state === "review" && (
        <>
          <h3 className={sheet.h}>Merge diff (against the branch before the merge)</h3>
          <ReviewTab boardKey={board.key} nn={ticket.nn} load={load} against="head" />
        </>
      )}
    </section>
  );
}

/**
 * The developer's decision on a resolved merge: Abort merge, or Commit merge —
 * the only commit and push the hub ever causes — behind an explicit confirm.
 * Leaving it is not clicking either.
 */
export function ConflictDecision({ board, ticket, api, waiting }: {
  board: Board;
  ticket: Ticket;
  api: ConflictApi;
  waiting: (method: string) => boolean;
}) {
  const [confirming, setConfirming] = useState(false);
  if (confirming)
    return (
      <>
        <p className={styles.confirm}>
          Commit merge commits the merge and pushes it to {ticket.branch || "the ticket's branch"} (never force). The
          PR's checks then re-run.
        </p>
        <button type="button" className={actions.secondary} onClick={() => setConfirming(false)}>
          Cancel
        </button>
        <button
          type="button"
          className={actions.primary}
          data-primary="true"
          disabled={waiting("finish_conflict")}
          aria-busy={waiting("finish_conflict")}
          onClick={async () => {
            if (await api.finishConflict(board.key, ticket.nn)) setConfirming(false);
          }}
        >
          {waiting("finish_conflict") && <Spinner />}Commit &amp; push
        </button>
      </>
    );
  return (
    <>
      <button
        type="button"
        className={actions.secondary}
        disabled={waiting("abort_conflict")}
        aria-busy={waiting("abort_conflict")}
        onClick={() => void api.abortConflict(board.key, ticket.nn)}
      >
        {waiting("abort_conflict") && <Spinner />}Abort merge
      </button>
      <button type="button" className={actions.primary} data-primary="true" onClick={() => setConfirming(true)}>
        Commit merge
      </button>
    </>
  );
}
