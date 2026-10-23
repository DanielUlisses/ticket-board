// The one store of hub-web: boards and devboxes from `GET /api/boards`, kept
// live by the /ws/ui events, plus the UI's own filters, page and toasts.
// Components read it through `view()` (a cached snapshot, stable between
// changes, so it plugs into useSyncExternalStore) and never keep their own copy.
import { COLUMN_META, type Group } from "../columns";
import type { Board, BoardsResponse, Devbox, Ticket, RpcResult, UiEvent } from "../protocol/types";

/** The Stage select and the pills: one filter, over the column groups. */
export type Stage = "all" | Group;

export type Counts = Record<Stage, number>;

/** The Devbox / Board selects (null: all) and the stage (select or pill). */
export interface Filter {
  devbox: string | null;
  board: string | null;
  stage: Stage;
}

export type Page = "cards" | "live";

export interface Toast {
  id: number;
  kind: "ok" | "error";
  text: string;
}

export interface View {
  page: Page;
  filter: Filter;
  /** Boards passing the filters, each with only its passing tickets. */
  boards: Board[];
  /** Ticket counts per stage under the Devbox and Board selects (not the stage). */
  counts: Counts;
  devboxes: Devbox[];
  /** Every non-archived board, unfiltered: the Devbox and Board select options. */
  allBoards: Board[];
  /** `ticketId`s of tickets that just moved (Live Board plays `pulseMove`). */
  justMoved: ReadonlySet<string>;
  toasts: Toast[];
  /** The ticket open in the sheet, read from the live (unfiltered) boards. */
  sheet: { board: Board; ticket: Ticket } | null;
}

interface State {
  page: Page;
  boards: Board[];
  devboxes: Devbox[];
  filter: Filter;
  justMoved: ReadonlySet<string>;
  toasts: Toast[];
  opened: { boardKey: string; nn: string } | null;
}

const NO_FILTER: Filter = { devbox: null, board: null, stage: "all" };

const STAGES: readonly Stage[] = ["all", "attention", "active", "frontier", "blocked", "done"];

const inStage = (stage: Stage, t: Ticket) => stage === "all" || COLUMN_META[t.col].group === stage;

/**
 * How long a ticket stays just-moved: kept above twice the pulse (2 × 1.1 s,
 * --pulse-duration in tokens.css). The store expires it itself so moves seen off-screen (Cards
 * page, filtered out, reduced motion) never pile up; animationend clears early.
 */
export const JUST_MOVED_MS = 3000;

export const ticketId = (boardKey: string, nn: string) => `${boardKey}#${nn}`;

function rpcToast(r: RpcResult): Omit<Toast, "id"> {
  const prefix = r.method ? `${r.method}: ` : "";
  if (r.ok) return { kind: "ok", text: prefix + (r.output_tail.trim() || "done") };
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
  };
  let cached: View | null = null;
  let nextToast = 1;
  const listeners = new Set<() => void>();
  const moveTimers = new Map<string, ReturnType<typeof setTimeout>>();

  function set(next: Partial<State>) {
    state = { ...state, ...next };
    // A selected board that was removed or archived can no longer be picked.
    const picked = state.filter.board;
    if (picked !== null && !state.boards.some((b) => b.key === picked && !b.hub_meta.archived))
      state = { ...state, filter: { ...state.filter, board: null } };
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
      case "rpc_result":
        toast(rpcToast(event));
        return;
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

  /** Non-archived boards under the Devbox and Board selects. */
  function selected(): Board[] {
    const { devbox, board } = state.filter;
    return visible().filter((b) => (devbox === null || b.devbox === devbox) && (board === null || b.key === board));
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
    const { stage } = state.filter;
    const boards = selected();
    return {
      page: state.page,
      filter: state.filter,
      boards:
        stage === "all"
          ? boards
          : boards
              .map((b) => ({ ...b, tickets: b.tickets.filter((t) => inStage(stage, t)) }))
              .filter((b) => b.tickets.length > 0),
      counts: Object.fromEntries(STAGES.map((st) => [st, count(boards, st)])) as Counts,
      devboxes: state.devboxes,
      allBoards: visible(),
      justMoved: state.justMoved,
      toasts: state.toasts,
      sheet: openedSheet(),
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
