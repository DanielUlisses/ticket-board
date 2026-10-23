import { Diff, Hunk, parseDiff } from "react-diff-view";
import "react-diff-view/style/index.css";
import styles from "./ReviewTab.module.css";

/**
 * One file's unified diff, rendered by react-diff-view. This module is loaded
 * lazily (ReviewTab), so the library and its CSS only ship once a diff is opened.
 * Line comments (ticket 26) would hook in here: `getChangeKey` + `widgets` on <Diff>.
 */
export default function ReviewDiff({ diff }: { diff: string }) {
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
  return (
    <div className={styles.diff} data-diff>
      <Diff viewType="unified" diffType={files[0].type} hunks={hunks}>
        {(hs) => hs.map((h) => <Hunk key={h.content} hunk={h} />)}
      </Diff>
    </div>
  );
}
