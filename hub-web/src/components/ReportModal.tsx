import { useEffect, useState } from "react";
import type { ReportResponse } from "../protocol/types";
import { Markdown } from "./Markdown";
import styles from "./ReportModal.module.css";

type Load = { state: "loading" } | { state: "error"; text: string } | { state: "ok"; report: ReportResponse };

/** Full Review & Reasoning: the ticket's report and raw body as sanitized markdown. Esc is handled by the sheet. */
export function ReportModal({
  boardKey,
  nn,
  load,
  onClose,
}: {
  boardKey: string;
  nn: string;
  load: (boardKey: string, nn: string) => Promise<ReportResponse>;
  onClose: () => void;
}) {
  const [result, setResult] = useState<Load>({ state: "loading" });
  useEffect(() => {
    let live = true;
    load(boardKey, nn).then(
      (report) => live && setResult({ state: "ok", report }),
      (err: Error) => live && setResult({ state: "error", text: err.message }),
    );
    return () => {
      live = false;
    };
  }, [boardKey, nn, load]);

  return (
    <div
      className={styles.backdrop}
      onClick={(e) => {
        e.stopPropagation();
        onClose();
      }}
    >
      <div
        className={styles.modal}
        role="dialog"
        aria-modal="true"
        aria-label="Full Review & Reasoning"
        onClick={(e) => e.stopPropagation()}
      >
        <header className={styles.head}>
          <strong>Full Review &amp; Reasoning</strong>
          <button type="button" className={styles.close} aria-label="Close report" onClick={onClose}>
            ✕
          </button>
        </header>
        <div className={styles.body}>
          {result.state === "loading" && <p className={styles.note}>Loading...</p>}
          {result.state === "error" && <p className={styles.note}>Could not load the report: {result.text}</p>}
          {result.state === "ok" && (
            <>
              <section aria-label="Report">
                {result.report.full_report_md ? (
                  <Markdown source={result.report.full_report_md} />
                ) : (
                  <p className={styles.note}>No report yet.</p>
                )}
              </section>
              <section aria-label="Ticket body">
                <h3 className={styles.sub}>Ticket</h3>
                {result.report.body !== null ? (
                  <Markdown source={result.report.body} />
                ) : (
                  <p className={styles.note}>No ticket body.</p>
                )}
              </section>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
