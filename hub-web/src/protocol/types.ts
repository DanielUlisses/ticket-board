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
  /** `-`, `no`, `yes:<how>` or `unknown:<why>` (board-lib.sh landed). */
  landed: string;
  note: string;
  hint: string;
  summary: string | null;
  reviewer_notes: string | null;
  full_report_md: string | null;
  manual_tasks: ManualTask[];
  synthesis_backend: SynthesisBackend | null;
  synthesis_status: SynthesisStatus;
  /** A durable rule the last synthesis run suggests for project memory; prefills "Propose to memory". */
  new_memory_rule?: string | null;
  /** Why the last synthesis run failed (synthesis_status failed). */
  synthesis_error?: string | null;
  /** The ticket's worktree on its box; empty before launch. The Review tab needs one. */
  worktree?: string;
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

/** `$defs/usage_window`. */
export interface UsageWindow {
  used_percentage: number;
  resets_at: Timestamp;
}

/** `$defs/usage`: unknown has null windows; stale is older than 30 min while agents run. */
export interface Usage {
  state: "ok" | "unknown" | "stale";
  five_hour: UsageWindow | null;
  seven_day: UsageWindow | null;
  updated_at: Timestamp | null;
}

/** `$defs/health` (subset). */
export interface Health {
  daemon_version: string;
  herdr: { ok: boolean; status: string };
  usage?: Usage;
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
  /** Structured results: pane_tail's `pane`, a manual launch's `over_cap`… */
  data?: Record<string, unknown>;
  devbox?: string;
  method?: string;
  board_key?: string;
  nn?: string;
}
/**
 * `$defs/synthesis_event` / `synthesis_failed`, relayed by the hub from the daemon.
 * nn null: the board brief (ticket 18). backend: the one about to run (started), that
 * produced the result (finished), or tried last (failed).
 */
export interface SynthesisEvent {
  type: "synthesis_started" | "synthesis_finished" | "synthesis_failed";
  board_key: string;
  nn: string | null;
  backend: SynthesisBackend | null;
  at: Timestamp;
  error?: string;
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
 * `GET /api/boards/{board_key}/tickets/{nn}/report` (`$defs/rest_report`).
 * `body` is the ticket file's raw text, null when the daemon didn't send it.
 */
export interface ReportResponse {
  board_key: string;
  nn: string;
  title: string;
  full_report_md: string | null;
  body: string | null;
}

/** One `ticket_moved` row of the activity endpoint (the hub's log). */
export interface ActivityEvent {
  from: Column;
  to: Column;
  at: Timestamp;
}

/** `GET /api/boards/{board_key}/tickets/{nn}/activity` (`$defs/rest_activity`), oldest first. */
export interface ActivityResponse {
  board_key: string;
  nn: string;
  events: ActivityEvent[];
}

/** One changed file of `get_diff` (`$defs/rest_diff` `files`); untracked files are `A`. */
export interface DiffFile {
  path: string;
  old_path?: string;
  status: "M" | "A" | "D" | "R";
  added: number;
  removed: number;
  binary: boolean;
  /** An untracked directory that is itself a git repo (listed with a trailing slash): no diff to show. */
  nested_repo?: boolean;
}

/** `GET /api/boards/{board_key}/tickets/{nn}/diff` (`$defs/rest_diff`): the file list. */
export interface DiffListResponse {
  board_key: string;
  nn: string;
  base: string;
  /** Content hash of the whole diff, untracked files included. */
  diff_hash: string;
  files: DiffFile[];
  /** The daemon listed only its first `diff_max_files` files; `total_files` is the real count. */
  truncated?: boolean;
  total_files?: number;
}

/** The same with `?file=`: that file's entry and its unified diff, or a stub (empty diff). */
export interface DiffFileResponse extends DiffFile {
  board_key: string;
  nn: string;
  base: string;
  diff_hash: string;
  diff: string;
  stub: "binary" | "over_cap" | "nested_repo" | null;
}

/** `$defs/params_launch_ticket` `type`: tk.sh launch --type. */
export type TicketType = "feat" | "fix" | "refactor" | "chore" | "docs" | "test" | "perf" | "ci";

export const TICKET_TYPES: readonly TicketType[] = ["feat", "fix", "refactor", "chore", "docs", "test", "perf", "ci"];

/** One rpc the hub sent for an action (`$defs/rest_action` `rpcs`). */
export interface SentRpc {
  id: string;
  devbox: string;
  method: string;
  board_key: string;
  nn: string;
}

/**
 * `POST /api/boards/{board_key}/tickets/{nn}/action` and `.../frontier` -> 202
 * (`$defs/rest_action`); each rpc's outcome arrives later as rpc_result.
 */
export interface ActionResponse {
  rpcs: SentRpc[];
  skipped?: { nn: string; reason: string }[];
}
