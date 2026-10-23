// Hand-written types for the parts of protocol/schema.json the web reads.
// The schema is the source of truth: when it changes, change these with it.
// Fields the UI does not read yet are left out; extra fields pass through.

/** `$defs/column`: ticket-skill's board_rows classification. */
export type Column = "needs" | "backlog" | "blocked" | "working" | "review" | "human" | "pr" | "done";

export const COLUMNS: readonly Column[] = [
  "needs",
  "backlog",
  "blocked",
  "working",
  "review",
  "human",
  "pr",
  "done",
];

/** RFC 3339 UTC with a `Z` suffix. */
export type Timestamp = string;

export type SynthesisBackend = "agy" | "cursor" | "claude";
export type SynthesisStatus = "none" | "running" | "ok" | "failed";

export interface PrCheck {
  n: string;
  s: "ok" | "pending" | "bad";
}

export interface Pr {
  number: number;
  url?: string;
  isDraft: boolean;
  mergeable: "MERGEABLE" | "CONFLICTING" | "UNKNOWN";
  review: string;
  checks: PrCheck[];
}

/** `$defs/manual_task`. */
export interface ManualTask {
  text: string;
  done: boolean;
  source: "synthesis" | "developer";
}

/** `$defs/ticket` (subset). */
export interface Ticket {
  nn: string;
  title: string;
  status: "open" | "in-progress" | "resolved";
  branch: string;
  model: string;
  effort: string;
  smodel: string;
  seffort: string;
  state: string;
  round: string;
  pr: Pr | null;
  checks: Record<string, string>;
  col: Column;
  note: string;
  hint: string;
  summary: string | null;
  reviewer_notes: string | null;
  full_report_md: string | null;
  manual_tasks: ManualTask[];
  synthesis_backend: SynthesisBackend | null;
  synthesis_status: SynthesisStatus;
}

/** `$defs/policy` (subset): `tk.sh policy <board>`, resolved. */
export interface Policy {
  forge: "github" | "azure" | "none";
  require_human_before_pr: boolean;
  auto_merge: boolean;
  night_shift: boolean;
  autonomy: "assist" | "auto";
  max_parallel: number;
  usage_gate_pct: number;
}

export type PolicySource = "default" | "box" | "repo" | "board" | "env" | "detected" | "no-remote";

/** The board-override keys the UI may write (`rest_set_policy`); null removes a key's override. */
export type PolicyEdit = {
  [K in "forge" | "require_human_before_pr" | "auto_merge" | "night_shift" | "autonomy"]?: Policy[K] | null;
};

/** `$defs/board` (subset). `stale`/`last_seen` are added by the hub. */
export interface Board {
  key: string;
  devbox: string;
  board_id: string;
  slug: string;
  repo: string;
  forge: "github" | "azure" | "none";
  project_memory: string | null;
  policy: Policy;
  /** Which layer each policy key came from; keys it omits are defaults. */
  policy_sources?: Partial<Record<keyof Policy, PolicySource>>;
  hub_meta: { archived: boolean };
  hash: string;
  exported_at: Timestamp;
  tickets: Ticket[];
  stale?: boolean;
  last_seen?: Timestamp | null;
}

/** `$defs/health` (subset). */
export interface Health {
  daemon_version: string;
  herdr: { ok: boolean; status: string };
  running: number;
}

/** A devbox as the UI knows it: `devbox_status` without its `type`. */
export interface Devbox {
  devbox: string;
  online: boolean;
  last_seen: Timestamp | null;
  health: Health | null;
  protocol_version?: string;
  refused_reason?: string;
}

export interface BoardSynced {
  type: "board_synced";
  board: Board;
}
export interface BoardRemoved {
  type: "board_removed";
  board_key: string;
}
export interface TicketMoved {
  type: "ticket_moved";
  board_key: string;
  nn: string;
  from: Column;
  to: Column;
  at: Timestamp;
}
export interface DevboxStatus extends Devbox {
  type: "devbox_status";
}
export interface RpcResult {
  type: "rpc_result";
  id: string;
  ok: boolean;
  exit_code: number | null;
  output_tail: string;
  reason?: string;
  devbox?: string;
  method?: string;
  board_key?: string;
  nn?: string;
}
/** synthesis_* arrive on /ws/ui too; the shell ignores them (tickets 17/18). */
export interface SynthesisEvent {
  type: "synthesis_started" | "synthesis_finished" | "synthesis_failed";
  board_key: string;
  nn: string | null;
}

/** Every message the hub sends on /ws/ui. */
export type UiEvent = BoardSynced | BoardRemoved | TicketMoved | DevboxStatus | RpcResult | SynthesisEvent;

/**
 * `GET /api/boards`. ASSUMPTION: ticket 08 describes it as "all boards/tickets +
 * devboxes" without a shape; this is the shape the web expects.
 */
export interface BoardsResponse {
  boards: Board[];
  devboxes: Devbox[];
}

/**
 * `GET /api/boards/{board_key}/tickets/{nn}/report`. ASSUMPTION: ticket 08
 * names the endpoint but not the shape; `body` is the raw ticket file.
 */
export interface ReportResponse {
  full_report_md: string | null;
  body: string;
}

/**
 * One row of `GET /api/boards/{board_key}/tickets/{nn}/activity` (the hub's
 * `ticket_moved` log, oldest first). ASSUMPTION: a bare array, shape as TicketMoved.
 */
export interface ActivityEvent {
  from: Column;
  to: Column;
  at: Timestamp;
}
