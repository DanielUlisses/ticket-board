// The eight columns as the boards show them, from the prototype's COLUMNS:
// order (the swimlane order), label, glyph, glyph variant and stage group.
import type { Column } from "./protocol/types";

/** The stage groups: the Stage select and the pills are one filter over them. */
export type Group = "attention" | "active" | "frontier" | "blocked" | "done";

/** Glyph styling: the five coloured variants (board.md), or plain. */
export type Variant = "needs" | "human" | "pr" | "working" | "review" | "none";

export interface ColumnMeta {
  label: string;
  glyph: string;
  variant: Variant;
  group: Group;
}

export const COLUMN_META: Record<Column, ColumnMeta> = {
  needs: { label: "NEEDS YOU", glyph: "!", variant: "needs", group: "attention" },
  backlog: { label: "FRONTIER", glyph: "○", variant: "none", group: "frontier" },
  blocked: { label: "BLOCKED", glyph: "⊘", variant: "none", group: "blocked" },
  working: { label: "WORKING", glyph: "◐", variant: "working", group: "active" },
  review: { label: "REVIEW", glyph: "◑", variant: "review", group: "active" },
  human: { label: "HUMAN GATE", glyph: "◉", variant: "human", group: "attention" },
  pr: { label: "PR / PUSHED", glyph: "⇡", variant: "pr", group: "attention" },
  done: { label: "DONE", glyph: "✓", variant: "none", group: "done" },
};

export const GROUPS: readonly { group: Group; label: string }[] = [
  { group: "attention", label: "Needs You / Human Gate" },
  { group: "active", label: "Active (Working / Review)" },
  { group: "frontier", label: "Frontier Ready" },
  { group: "blocked", label: "Blocked" },
  { group: "done", label: "Done" },
];

/** `2026-10-23T13:10:00Z` -> `2026-10-23 13:10Z` (UTC, as the hub sends it). */
export const formatSeen = (ts: string) => `${ts.slice(0, 10)} ${ts.slice(11, 16)}Z`;
