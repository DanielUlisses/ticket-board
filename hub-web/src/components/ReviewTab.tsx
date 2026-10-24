import { lazy, type ReactNode, Suspense, useEffect, useId, useState } from "react";
import type { HubClient } from "../api/client";
import type { DiffFile, DiffFileResponse, HumanReview, ReviewComment } from "../protocol/types";
import styles from "./ReviewTab.module.css";

/** The review calls: draft comments, Approve, Request changes. */
export type ReviewActions = Pick<HubClient, "setReviewComments" | "approveReview" | "requestChanges">;

const NO_COMMENTS: ReviewComment[] = [];

/** Where the comment editor is open: under a diff line, or in the draft list. */
type Editing = { file: string; line: number; where: "line" | "list" };

/** The editor for one line's comment: Save keeps the trimmed text, empty is not allowed. */
function CommentEditor({ file, line, initial, onSave, onCancel }: {
  file: string;
  line: number;
  initial: string;
  onSave: (text: string) => void;
  onCancel: () => void;
}) {
  const [text, setText] = useState(initial);
  return (
    <div className={styles.editor}>
      <textarea
        className={styles.commentText}
        aria-label={`Comment on ${file}:${line}`}
        rows={3}
        autoFocus
        value={text}
        onChange={(e) => setText(e.target.value)}
      />
      <div className={styles.buttons}>
        <button type="button" className={styles.button} onClick={onCancel}>
          Cancel
        </button>
        <button type="button" className={styles.button} disabled={!text.trim()} onClick={() => onSave(text.trim())}>
          Save comment
        </button>
      </div>
    </div>
  );
}

// The diff renderer (react-diff-view + its CSS) loads on the first expanded file.
const ReviewDiff = lazy(() => import("./ReviewDiff"));

type FileState = { state: "loading" } | { state: "error"; message: string } | { state: "done"; data: DiffFileResponse };

function FileDiff({ file, state, onLine, lineWidget }: {
  file: DiffFile;
  state: FileState | undefined;
  onLine?: (line: number) => void;
  lineWidget?: (line: number) => ReactNode;
}) {
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
      <ReviewDiff diff={data.diff} onLine={onLine} lineWidget={lineWidget} />
    </Suspense>
  );
}

/** What the review bar says of the approval against the diff as loaded. */
function approvalText(approval: HumanReview | null, hash: string | null): string {
  if (!approval?.approved) return "Not approved yet.";
  if (!hash) return "Approved; checking it against the diff...";
  if (approval.diff_hash !== hash) return "The diff changed since approval: review it again.";
  return `✓ Approved at ${approval.at.slice(11, 16)}Z.`;
}

/**
 * The ticket's uncommitted diff against its base: the file list with status and
 * stats, each file's unified diff loaded on its first expand (and kept). With
 * `actions`, the hub review: tap a line to draft a comment on it (the draft lives
 * in the box's hub-meta, `comments`), Request changes, and Approve the diff as
 * loaded (an `approval` of another diff_hash is void). Without, read-only.
 */
export function ReviewTab({ boardKey, nn, load, actions, comments = NO_COMMENTS, approval = null }: {
  boardKey: string;
  nn: string;
  load: HubClient["diff"];
  actions?: ReviewActions;
  comments?: ReviewComment[];
  approval?: HumanReview | null;
}) {
  const uid = useId();
  const [hash, setHash] = useState<string | null>(null);
  // The draft as last saved here, until the box's copy (the next snapshot) replaces it.
  const [draft, setDraft] = useState(comments);
  const [editing, setEditing] = useState<Editing | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => setDraft(comments), [comments]);
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
        setHash(r.diff_hash);
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

  const at = (file: string, line: number) => draft.find((c) => c.file === file && c.line === line);

  function save(next: ReviewComment[]) {
    if (!actions) return;
    setDraft(next);
    setEditing(null);
    void actions.setReviewComments(boardKey, nn, next).then((ok) => ok || setDraft(comments));
  }
  function put(file: string, line: number, text: string) {
    const found = at(file, line);
    save(found ? draft.map((c) => (c === found ? { ...c, text } : c)) : [...draft, { file, line, text }]);
  }
  const remove = (c: ReviewComment) => save(draft.filter((x) => x !== c));

  async function run(call: () => Promise<boolean>) {
    setBusy(true);
    await call();
    setBusy(false);
  }

  /** Under a diff line: the editor when it is open there, else its draft comment. */
  const lineWidget = (file: string) => (line: number) => {
    const c = at(file, line);
    if (editing?.where === "line" && editing.file === file && editing.line === line)
      return (
        <CommentEditor
          file={file}
          line={line}
          initial={c?.text ?? ""}
          onSave={(text) => put(file, line, text)}
          onCancel={() => setEditing(null)}
        />
      );
    return c ? <p className={styles.comment}>{c.text}</p> : null;
  };

  if (error) return <p className={styles.note}>Diff unavailable: {error}</p>;
  if (!files) return <p className={styles.note}>Loading the diff...</p>;
  if (files.length === 0) return <p className={styles.note}>No uncommitted changes against the base.</p>;
  const approvedNow = !!approval?.approved && approval.diff_hash === hash;
  return (
    <>
      {actions && (
        <section className={styles.review} aria-label="Review">
          <p className={styles.approval} role="status" aria-label="Review status" data-approved={approvedNow}>
            {approvalText(approval, hash)}
          </p>
          {draft.length > 0 && (
            <ul className={styles.drafts} aria-label="Draft comments">
              {draft.map((c) => (
                <li key={`${c.file}:${c.line}`}>
                  {editing?.where === "list" && editing.file === c.file && editing.line === c.line ? (
                    <CommentEditor
                      file={c.file}
                      line={c.line}
                      initial={c.text}
                      onSave={(text) => put(c.file, c.line, text)}
                      onCancel={() => setEditing(null)}
                    />
                  ) : (
                    <>
                      <span className={styles.draftText}>
                        {c.file}:{c.line} — {c.text}
                      </span>
                      <span className={styles.buttons}>
                        <button
                          type="button"
                          className={styles.button}
                          onClick={() => setEditing({ file: c.file, line: c.line, where: "list" })}
                        >
                          Edit
                        </button>
                        <button type="button" className={styles.button} onClick={() => remove(c)}>
                          Delete
                        </button>
                      </span>
                    </>
                  )}
                </li>
              ))}
            </ul>
          )}
          <div className={styles.buttons}>
            <button
              type="button"
              className={styles.button}
              disabled={busy || draft.length === 0}
              onClick={() => void run(() => actions.requestChanges(boardKey, nn))}
            >
              Request changes{draft.length > 0 ? ` (${draft.length})` : ""}
            </button>
            <button
              type="button"
              className={styles.button}
              data-primary="true"
              disabled={busy || !hash || approvedNow}
              onClick={() => hash && void run(() => actions.approveReview(boardKey, nn, hash))}
            >
              ✓ Approve
            </button>
          </div>
        </section>
      )}
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
                <FileDiff
                  file={f}
                  state={loaded[f.path]}
                  onLine={actions && ((line) => setEditing({ file: f.path, line, where: "line" }))}
                  lineWidget={actions && lineWidget(f.path)}
                />
              </section>
            )}
          </li>
        );
      })}
      </ul>
    </>
  );
}
