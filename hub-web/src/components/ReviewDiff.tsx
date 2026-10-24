import type { ReactNode } from "react";
import { computeNewLineNumber, computeOldLineNumber, Diff, getChangeKey, Hunk, parseDiff } from "react-diff-view";
import type { ChangeData, HunkData } from "react-diff-view";
import "react-diff-view/style/index.css";
import styles from "./ReviewTab.module.css";

/** The line a comment on `change` names: the new file's, the old file's on a removed line. */
export function commentLine(change: ChangeData): number {
  return change.type === "delete" ? computeOldLineNumber(change) : computeNewLineNumber(change);
}

/** line -> the change a comment on it shows under: the new file's line, else a removed line with that old number. */
function lineOwners(hunks: HunkData[]): Map<number, string> {
  const owners = new Map<number, string>();
  const changes = hunks.flatMap((h) => h.changes);
  for (const c of changes) if (c.type !== "delete") owners.set(computeNewLineNumber(c), getChangeKey(c));
  for (const c of changes) {
    const line = computeOldLineNumber(c);
    if (c.type === "delete" && !owners.has(line)) owners.set(line, getChangeKey(c));
  }
  return owners;
}

/**
 * One file's unified diff, rendered by react-diff-view. This module is loaded
 * lazily (ReviewTab), so the library and its CSS only ship once a diff is opened.
 * With `onLine`, tapping a line asks to comment on it; `lineWidget` renders what
 * shows under a line (its draft comment, or the comment editor).
 */
export default function ReviewDiff({
  diff,
  onLine,
  lineWidget,
}: {
  diff: string;
  onLine?: (line: number) => void;
  lineWidget?: (line: number) => ReactNode;
}) {
  // The parser wants a `diff --git` header; get_diff always sends one, but be lenient.
  const head = diff.startsWith("--- ") ? "" : "--- a/f\n+++ b/f\n";
  const text = diff.startsWith("diff --git") ? diff : `diff --git a/f b/f\n${head}${diff}`;
  let files;
  try {
    files = parseDiff(text);
  } catch {
    return <p className={styles.note}>This diff could not be parsed.</p>;
  }
  const hunks = files.flatMap((f) => f.hunks);
  if (hunks.length === 0 && /^Binary files /m.test(diff))
    return <p className={styles.note}>Binary file, not shown.</p>;
  if (hunks.length === 0) return <p className={styles.note}>No textual changes (mode or rename only).</p>;
  const widgets: Record<string, ReactNode> = {};
  if (lineWidget)
    for (const [line, key] of lineOwners(hunks)) {
      const w = lineWidget(line);
      if (w) widgets[key] = w;
    }
  const codeEvents = onLine && {
    onClick: ({ change }: { change: ChangeData | null }) => change && onLine(commentLine(change)),
  };
  return (
    <div className={styles.diff} data-diff data-commentable={onLine ? "true" : undefined}>
      <Diff viewType="unified" diffType={files[0].type} hunks={hunks} widgets={widgets} codeEvents={codeEvents}>
        {(hs) => hs.map((h) => <Hunk key={h.content} hunk={h} />)}
      </Diff>
    </div>
  );
}
