// The one store of hub-web: boards and devboxes from `GET /api/boards`, kept
// live by the /ws/ui events, plus the UI's own filters, page and toasts, the
// actions waiting on their rpc_result and the agent panes (pane_tail).
// Components read it through `view()` (a cached snapshot, stable between
// changes, so it plugs into useSyncExternalStore) and never keep their own copy.
import { COLUMN_META, GROUPS, type Group } from "../columns";
import type { Board, BoardsResponse, Devbox, Ticket, RpcResult, SentRpc, UiEvent } from "../protocol/types";

/** The stage presets (pills) and the keys of `Counts`: "all" is every group. */
export type Stage = "all" | Group;

export type Counts = Record<Stage, number>;

/**
 * The Devbox select (null: all), the Stage selection (the pills are presets of
 * it) and the Board picks: only what the user ticked or unticked, by board key.
 * A board with no pick follows the default (ticked unless archived), so boards
 * arriving later need no entry and an unticked one stays unticked.
 */
export interface Filter {
  devbox: string | null;
  boards: Readonly<Record<string, boolean>>;
  stages: Group[];
}

const ALL_GROUPS: Group[] = GROUPS.map((g) => g.group);

/** The stage set a pill stands for: "all" is every group including Done. */
export const stagesFor = (stage: Stage): Group[] => (stage === "all" ? [...ALL_GROUPS] : [stage]);

/** The pill whose preset exactly matches the stage set, if any. */
export const presetOf = (stages: readonly Group[]): Stage | null =>
  STAGES.find((st) => {
    const want = stagesFor(st);
    return want.length === stages.length && want.every((g) => stages.includes(g));
  }) ?? null;

/** Whether the Board selection shows this board (its pick, else ticked unless archived). */
export const isBoardShown = (filter: Filter, board: Board) => filter.boards[board.key] ?? !board.hub_meta.archived;

export type Page = "cards" | "live";

export interface Toast {
  id: number;
  kind: "ok" | "warn" | "error";
  text: string;
}

/** An agent's recent output (pane_tail), or why there is none. */
export interface Pane {
  ok: boolean;
  text: string;
  reason: string | null;
}

export interface View {
  page: Page;
  filter: Filter;
  /** Boards passing the filters, each with only its passing tickets. */
  boards: Board[];
  /** Ticket counts per stage under the Devbox select and Board ticks (not the stage). */
  counts: Counts;
  devboxes: Devbox[];
  /** Every non-archived board, unfiltered: the Devbox and Board options. */
  allBoards: Board[];
  /** Archived boards, unfiltered: the Board dropdown's "Archived" section. */
  archivedBoards: Board[];
  /** `ticketId`s of tickets that just moved (Live Board plays `pulseMove`). */
  justMoved: ReadonlySet<string>;
  toasts: Toast[];
  /** The ticket open in the sheet, read from the live (unfiltered) boards. */
  sheet: { board: Board; ticket: Ticket } | null;
  /** Actions sent and waiting on their rpc_result (spinners). */
  pending: SentRpc[];
  /** ok of every tracked action that has finished, by rpc id. */
  settled: Readonly<Record<string, boolean>>;
  /** The last pane_tail per ticket, by `ticketId`. */
  panes: Readonly<Record<string, Pane>>;
}

interface State {
  page: Page;
  boards: Board[];
  devboxes: Devbox[];
  filter: Filter;
  justMoved: ReadonlySet<string>;
  toasts: Toast[];
  opened: { boardKey: string; nn: string } | null;
  pending: SentRpc[];
  settled: Record<string, boolean>;
  panes: Record<string, Pane>;
}

/** Every group except Done: running, frontier and blocked tickets show, finished ones don't. */
const NO_FILTER: Filter = { devbox: null, boards: {}, stages: ALL_GROUPS.filter((g) => g !== "done") };

const STAGES: readonly Stage[] = ["all", "attention", "active", "frontier", "blocked", "done"];

const inStage = (stage: Stage, t: Ticket) => stage === "all" || COLUMN_META[t.col].group === stage;
const inStages = (stages: readonly Group[], t: Ticket) => stages.includes(COLUMN_META[t.col].group);

/**
 * How long a ticket stays just-moved: kept above twice the pulse (2 × 1.1 s,
 * --pulse-duration in tokens.css). The store expires it itself so moves seen off-screen (Cards
 * page, filtered out, reduced motion) never pile up; animationend clears early.
 */
export const JUST_MOVED_MS = 3000;

/**
 * How long a tracked action stays pending without its rpc_result: the hub's rpc
 * timeout for ticket actions (120 s, TIMEOUT in hub.py) plus a margin. Past it,
 * the result was lost (a /ws/ui gap) and the spinner stops, with a warning.
 */
export const PENDING_EXPIRY_MS = 135_000;

/** How many finished actions `settled` remembers. */
const SETTLED_KEPT = 200;

export const ticketId = (boardKey: string, nn: string) => `${boardKey}#${nn}`;

/**
 * The result the hub (hub.py `_failed`) or the daemon sends when an rpc ran out
 * of time: nothing ran to report, only `timed out after <N>s`. A launch_ticket
 * like that may still be queued or running on the box (the daemon never cancels
 * a launch), so it is not a failure yet.
 */
const RPC_TIMED_OUT = /^timed out after \d+(\.\d+)?s$/;
const timedOut = (r: RpcResult) =>
  !r.ok && r.exit_code === null && r.output_tail === "" && RPC_TIMED_OUT.test(r.reason ?? "");

function rpcToast(r: RpcResult): Omit<Toast, "id"> {
  const prefix = r.method ? `${r.method}: ` : "";
  const d = r.data ?? {};
  if (r.ok && d.over_cap === true)
    return {
      kind: "warn",
      text: `${prefix}manual launch over max_parallel: ${d.running} running, cap ${d.max_parallel}`,
    };
  if (r.ok) return { kind: "ok", text: prefix + (r.output_tail.trim() || "done") };
  if (r.method === "launch_ticket" && timedOut(r))
    return { kind: "warn", text: `launch_ticket #${r.nn ?? "?"}: still running on the box, may still launch; watch the board` };
  const why = r.reason || r.output_tail.trim() || `exit ${r.exit_code ?? "?"}`;
  return { kind: "error", text: `${prefix}failed: ${why}` };
}

export function createStore() {
  let state: State = {
    page: "cards",
    boards: [],
    devboxes: [],
    filter: NO_FILTER,
    justMoved: new Set(),
    toasts: [],
    opened: null,
    pending: [],
    settled: {},
    panes: {},
  };
  let cached: View | null = null;
  let nextToast = 1;
  const listeners = new Set<() => void>();
  const moveTimers = new Map<string, ReturnType<typeof setTimeout>>();
  const pendingTimers = new Map<string, ReturnType<typeof setTimeout>>();

  function expire(id: string) {
    pendingTimers.delete(id);
    const lost = state.pending.find((p) => p.id === id);
    if (!lost) return;
    set({ pending: state.pending.filter((p) => p.id !== id) });
    toast({ kind: "warn", text: `${lost.method} #${lost.nn}: no result came back; check the board` });
  }

  function set(next: Partial<State>) {
    state = { ...state, ...next };
    // An opened ticket whose board or ticket is gone stays closed, even if it returns.
    const o = state.opened;
    if (o && !state.boards.some((b) => b.key === o.boardKey && b.tickets.some((t) => t.nn === o.nn)))
      state = { ...state, opened: null };
    cached = null;
    listeners.forEach((l) => l());
  }

  const mapBoards = (pred: (b: Board) => boolean, fn: (b: Board) => Board) =>
    state.boards.map((b) => (pred(b) ? fn(b) : b));

  function apply(event: UiEvent) {
    switch (event.type) {
      case "board_synced": {
        const known = state.boards.some((b) => b.key === event.board.key);
        set({
          boards: known
            ? mapBoards((b) => b.key === event.board.key, () => event.board)
            : [...state.boards, event.board],
        });
        return;
      }
      case "board_removed":
        set({ boards: state.boards.filter((b) => b.key !== event.board_key) });
        return;
      case "ticket_moved": {
        const board = state.boards.find((b) => b.key === event.board_key);
        if (!board?.tickets.some((t) => t.nn === event.nn)) return;
        const id = ticketId(event.board_key, event.nn);
        set({
          boards: mapBoards(
            (b) => b === board,
            (b) => ({ ...b, tickets: b.tickets.map((t) => (t.nn === event.nn ? { ...t, col: event.to } : t)) }),
          ),
          justMoved: new Set([...state.justMoved, id]),
        });
        clearTimeout(moveTimers.get(id));
        moveTimers.set(id, setTimeout(() => clearMoved(id), JUST_MOVED_MS));
        return;
      }
      case "devbox_status": {
        const { type: _type, ...box } = event;
        const known = state.devboxes.some((d) => d.devbox === box.devbox);
        set({
          devboxes: known
            ? state.devboxes.map((d) => (d.devbox === box.devbox ? box : d))
            : [...state.devboxes, box],
          boards: mapBoards(
            (b) => b.devbox === box.devbox,
            (b) => ({ ...b, stale: !box.online, last_seen: box.last_seen ?? b.last_seen }),
          ),
        });
        return;
      }
      case "rpc_result": {
        clearTimeout(pendingTimers.get(event.id));
        pendingTimers.delete(event.id);
        const pending = state.pending.filter((p) => p.id !== event.id);
        if (event.method === "pane_tail" && event.board_key && event.nn) {
          // A poll: the pane shows it, no toast.
          const pane = event.data?.pane;
          set({
            pending,
            panes: {
              ...state.panes,
              [ticketId(event.board_key, event.nn)]: {
                ok: event.ok,
                text: typeof pane === "string" ? pane : "",
                reason: event.ok ? null : event.reason || event.output_tail.trim() || `exit ${event.exit_code ?? "?"}`,
              },
            },
          });
          return;
        }
        // Also kept for a result that beats its POST's answer (track then ignores it).
        const settled = Object.entries({ ...state.settled, [event.id]: event.ok }).slice(-SETTLED_KEPT);
        set({ pending, settled: Object.fromEntries(settled) });
        toast(rpcToast(event));
        return;
      }
      default:
        // synthesis_* belong to the ticket sheet and board panel (tickets 17, 18).
        return;
    }
  }

  function clearMoved(id: string) {
    clearTimeout(moveTimers.get(id));
    moveTimers.delete(id);
    if (!state.justMoved.has(id)) return;
    const next = new Set(state.justMoved);
    next.delete(id);
    set({ justMoved: next });
  }

  function toast(t: Omit<Toast, "id">) {
    set({ toasts: [...state.toasts, { ...t, id: nextToast++ }] });
  }

  const visible = () => state.boards.filter((b) => !b.hub_meta.archived);

  /** Boards under the Devbox select and the Board ticks (an archived one only when ticked). */
  function selected(): Board[] {
    const { devbox } = state.filter;
    return state.boards.filter((b) => (devbox === null || b.devbox === devbox) && isBoardShown(state.filter, b));
  }

  const count = (boards: Board[], stage: Stage) =>
    boards.reduce((n, b) => n + b.tickets.filter((t) => inStage(stage, t)).length, 0);

  function openedSheet(): View["sheet"] {
    if (!state.opened) return null;
    const board = state.boards.find((b) => b.key === state.opened!.boardKey);
    const ticket = board?.tickets.find((t) => t.nn === state.opened!.nn);
    return board && ticket ? { board, ticket } : null;
  }

  function compute(): View {
    const { stages } = state.filter;
    const boards = selected();
    return {
      page: state.page,
      filter: state.filter,
      boards:
        stages.length === ALL_GROUPS.length
          ? boards
          : boards
              .map((b) => ({ ...b, tickets: b.tickets.filter((t) => inStages(stages, t)) }))
              .filter((b) => b.tickets.length > 0),
      counts: Object.fromEntries(STAGES.map((st) => [st, count(boards, st)])) as Counts,
      devboxes: state.devboxes,
      allBoards: visible(),
      archivedBoards: state.boards.filter((b) => b.hub_meta.archived),
      justMoved: state.justMoved,
      toasts: state.toasts,
      sheet: openedSheet(),
      pending: state.pending,
      settled: state.settled,
      panes: state.panes,
    };
  }

  return {
    load(data: BoardsResponse) {
      set({ boards: data.boards, devboxes: data.devboxes });
    },
    apply,
    setFilter(patch: Partial<Filter>) {
      set({ filter: { ...state.filter, ...patch } });
    },
    setPage(page: Page) {
      set({ page });
    },
    clearMoved,
    openTicket(boardKey: string, nn: string) {
      set({ opened: { boardKey, nn } });
    },
    closeTicket() {
      set({ opened: null });
    },
    toast,
    /** Actions the hub accepted: pending (a spinner) until their rpc_result, or PENDING_EXPIRY_MS. */
    track(rpcs: SentRpc[]) {
      const fresh = rpcs.filter((r) => !(r.id in state.settled) && !state.pending.some((p) => p.id === r.id));
      if (fresh.length === 0) return;
      for (const r of fresh) pendingTimers.set(r.id, setTimeout(() => expire(r.id), PENDING_EXPIRY_MS));
      set({ pending: [...state.pending, ...fresh] });
    },
    dismiss(id: number) {
      set({ toasts: state.toasts.filter((t) => t.id !== id) });
    },
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    view(): View {
      return (cached ??= compute());
    },
  };
}

export type Store = ReturnType<typeof createStore>;
