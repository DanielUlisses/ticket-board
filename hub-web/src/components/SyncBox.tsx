import { useState } from "react";
import { formatSeen } from "../columns";
import type { Devbox, SyncResult } from "../protocol/types";
import styles from "./SyncBox.module.css";

/** Starts `devbox sync` on a box; `force` skips the daemon's refusal while tickets run. */
export type OnSync = (devbox: string, force: boolean) => Promise<unknown> | void;

/** The last sync in a word: still running, ok, or failed (exit code, or interrupted: no exit code and its unit gone). */
export function syncState(last: SyncResult): { state: "running" | "ok" | "failed"; text: string } {
  if (last.exit_code === null && last.interrupted) return { state: "failed", text: "sync interrupted" };
  if (last.exit_code === null) return { state: "running", text: "syncing…" };
  if (last.exit_code === 0) return { state: "ok", text: "sync ok" };
  return { state: "failed", text: `sync failed (exit ${last.exit_code})` };
}

/** ⟳ Sync box on a devbox sub-pill: a confirm dialog that also shows the last sync's result, log tail and version. */
export function SyncBox({ name, box, onSync }: { name: string; box: Devbox | undefined; onSync: OnSync }) {
  const [open, setOpen] = useState(false);
  const health = box?.health;
  const last = health?.last_sync ?? null;
  const running = health?.running ?? 0;
  const lastState = last && syncState(last);
  const syncing = lastState?.state === "running";
  const confirm = () => {
    setOpen(false);
    void onSync(name, running > 0);
  };
  return (
    <>
      <button
        type="button"
        className={styles.button}
        disabled={!box?.online}
        title={`Upgrade ${name} with devbox sync`}
        onClick={() => setOpen(true)}
      >
        ⟳ Sync box
      </button>
      {lastState && (
        <span className={styles.result} data-sync={lastState.state}>
          {lastState.text}
        </span>
      )}
      {last && lastState && lastState.state !== "ok" && last.log_tail && (
        <details className={styles.logPanel} data-sync={lastState.state}>
          <summary>Sync log</summary>
          <pre className={styles.log}>{last.log_tail}</pre>
        </details>
      )}
      {open && (
        <div className={styles.backdrop} onClick={() => setOpen(false)}>
          <div className={styles.modal} role="dialog" aria-modal="true" aria-label={`Sync ${name}`} onClick={(e) => e.stopPropagation()}>
            <strong>Sync {name}?</strong>
            <p className={styles.note}>
              Runs <code>devbox sync</code> detached, so it survives the daemon being reinstalled. The result
              shows here once the daemon reconnects.
            </p>
            {running > 0 && (
              <p className={styles.warn}>
                {running === 1 ? "1 ticket is working or in review" : `${running} tickets are working or in review`} on this box; a sync can disturb them.
              </p>
            )}
            {last && lastState && (
              <section aria-label="Last sync">
                <p className={styles.note}>
                  Last sync: <strong data-sync={lastState.state}>{lastState.text}</strong> · started {formatSeen(last.started_at)} · daemon {health?.daemon_version}
                </p>
                {last.log_tail && <pre className={styles.log}>{last.log_tail}</pre>}
              </section>
            )}
            <span className={styles.actions}>
              <button type="button" onClick={() => setOpen(false)}>
                Cancel
              </button>
              <button type="button" className={styles.go} onClick={confirm} disabled={syncing}>
                {running > 0 ? "Sync anyway" : "Sync"}
              </button>
            </span>
          </div>
        </div>
      )}
    </>
  );
}
