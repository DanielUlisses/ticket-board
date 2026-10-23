import { lazy, Suspense, useEffect, useId, useState } from "react";
import type { HubClient } from "../api/client";
import type { DiffFile, DiffFileResponse } from "../protocol/types";
import styles from "./ReviewTab.module.css";

// The diff renderer (react-diff-view + its CSS) loads on the first expanded file.
const ReviewDiff = lazy(() => import("./ReviewDiff"));

type FileState = { state: "loading" } | { state: "error"; message: string } | { state: "done"; data: DiffFileResponse };

function FileDiff({ file, state }: { file: DiffFile; state: FileState | undefined }) {
  if (!state || state.state === "loading") return <p className={styles.note}>Loading...</p>;
  if (state.state === "error") return <p className={styles.note}>{state.message}</p>;
  const { data } = state;
  const stats = `+${data.added} -${data.removed}`;
  if (data.stub === "binary") return <p className={styles.note}>Binary file, not shown ({stats}).</p>;
  if (data.stub === "nested_repo") return <p className={styles.note}>Nested git repository, contents not shown.</p>;
  if (data.stub === "over_cap") return <p className={styles.note}>Diff too large to show ({stats}).</p>;
  if (!data.diff) return <p className={styles.note}>No textual changes in {file.path}.</p>;
  return (
    <Suspense fallback={<p className={styles.note}>Loading...</p>}>
      <ReviewDiff diff={data.diff} />
    </Suspense>
  );
}

/**
 * The ticket's uncommitted diff against its base, read-only: the file list with
 * status and stats, each file's unified diff loaded on its first expand (and kept).
 */
export function ReviewTab({ boardKey, nn, load }: { boardKey: string; nn: string; load: HubClient["diff"] }) {
  const uid = useId();
  const [files, setFiles] = useState<DiffFile[] | null>(null);
  const [truncated, setTruncated] = useState(false); // the daemon cut the list short
  const [total, setTotal] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set());
  const [loaded, setLoaded] = useState<Record<string, FileState>>({});

  useEffect(() => {
    let live = true;
    setFiles(null);
    setTotal(null);
    setTruncated(false);
    setError(null);
    load(boardKey, nn).then(
      (r) => {
        if (!live) return;
        setFiles(r.files);
        setTotal(r.truncated ? (r.total_files ?? null) : null);
        setTruncated(!!r.truncated);
      },
      (e: Error) => live && setError(e.message),
    );
    return () => {
      live = false;
    };
  }, [load, boardKey, nn]);

  function toggle(path: string) {
    const next = new Set(open);
    if (next.has(path)) next.delete(path);
    else {
      next.add(path);
      if (loaded[path]?.state !== "done" && loaded[path]?.state !== "loading") {
        setLoaded((m) => ({ ...m, [path]: { state: "loading" } }));
        load(boardKey, nn, path).then(
          (data) => setLoaded((m) => ({ ...m, [path]: { state: "done", data } })),
          (e: Error) => setLoaded((m) => ({ ...m, [path]: { state: "error", message: e.message } })),
        );
      }
    }
    setOpen(next);
  }

  if (error) return <p className={styles.note}>Diff unavailable: {error}</p>;
  if (!files) return <p className={styles.note}>Loading the diff...</p>;
  if (files.length === 0) return <p className={styles.note}>No uncommitted changes against the base.</p>;
  return (
    <>
      {truncated && (
        <p className={styles.note}>
          Showing the first {files.length} of {total ?? "more"} changed files; the rest are not listed.
        </p>
      )}
      <ul className={styles.files}>
      {files.map((f, i) => {
        const panelId = `${uid}-diff-${i}`;
        const expanded = open.has(f.path);
        return (
          <li key={f.path} className={styles.file}>
            <button
              type="button"
              className={styles.row}
              aria-expanded={expanded}
              aria-controls={panelId}
              onClick={() => toggle(f.path)}
            >
              <span className={styles.status} data-status={f.status}>
                {f.status}
              </span>
              <span className={styles.path}>{f.old_path ? `${f.old_path} → ${f.path}` : f.path}</span>
              <span className={styles.stats}>
                {f.nested_repo ? (
                  "repo"
                ) : f.binary ? (
                  "bin"
                ) : (
                  <>
                    <span className={styles.plus}>+{f.added}</span> <span className={styles.minus}>-{f.removed}</span>
                  </>
                )}
              </span>
            </button>
            {expanded && (
              <section id={panelId} role="region" aria-label={f.path} className={styles.panel}>
                <FileDiff file={f} state={loaded[f.path]} />
              </section>
            )}
          </li>
        );
      })}
      </ul>
    </>
  );
}
