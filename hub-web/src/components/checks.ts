import type { Ticket } from "../protocol/types";

/** A ticket's checks metric: coordinator checks (tests, criteria, review), then CI on its PR. */
export function checksMetric(t: Ticket): string {
  const out: string[] = [];
  const c = t.checks;
  if (c.tests) out.push(`tests ${c.tests}`);
  if (c.criteria) out.push(c.criteria);
  if (c.review) out.push(c.review === "ok" ? "rev ✓" : `rev ${c.review}`);
  if (t.pr && t.pr.checks.length > 0) {
    const ok = t.pr.checks.filter((k) => k.s === "ok").length;
    out.push(`ci ${ok}/${t.pr.checks.length}`);
  }
  return out.join(" · ");
}

/** The model/effort a ticket runs with; `~` when only suggested (not launched). */
export function modelTag(t: Ticket): string | null {
  if (t.model !== "") return `${t.model}/${t.effort || "-"}`;
  if (t.status === "resolved" || t.smodel === "") return null;
  return `~${t.smodel}${t.seffort ? `/${t.seffort}` : ""}`;
}
