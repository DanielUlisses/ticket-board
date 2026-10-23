import { describe, expect, it, vi } from "vitest";
import type { Board, DevboxStatus, TicketMoved } from "../protocol/types";
import { boardsResponse, fixture } from "../test/fixtures";
import { createStore, isBoardShown, PENDING_EXPIRY_MS, presetOf, stagesFor, ticketId } from "./store";

const loaded = () => {
  const store = createStore();
  store.load(boardsResponse());
  return store;
};

describe("store", () => {
  it("counts the loaded boards by stage: Needs You is needs + human gate + PR", () => {
    expect(loaded().view().counts).toEqual({ all: 14, attention: 3, active: 2, frontier: 1, blocked: 7, done: 1 });
  });

  const nns = (store: ReturnType<typeof createStore>) =>
    store.view().boards.map((b) => [b.key, b.tickets.map((t) => t.nn)]);

  it("by default every group but Done shows, and Done tickets stay out", () => {
    const store = loaded();
    expect(store.view().filter.stages).toEqual(["attention", "active", "frontier", "blocked"]);
    expect(nns(store).flatMap(([, n]) => n)).not.toContain("04");
    expect(store.view().counts.done).toBe(1);
  });

  it("stage selections combine: a ticket passes when its group is selected", () => {
    const store = loaded();
    store.setFilter({ stages: ["frontier", "done"] });
    expect(nns(store)).toEqual([
      ["dev-daniel/ticket-skill/tk-hub", ["04"]],
      ["dev-daniel/ticket-board/tk-hub", ["27"]],
    ]);
  });

  it("a stage keeps only its tickets and drops boards left empty", () => {
    const store = loaded();
    store.setFilter({ stages: ["attention"] });
    expect(nns(store)).toEqual([
      ["dev-daniel/ticket-skill/tk-hub", ["01", "03"]],
      ["dev-daniel/devboxes/tk-hub", ["05"]],
    ]);
  });

  it("counts follow the Board selection but not the stage", () => {
    const store = loaded();
    store.setFilter({ boards: { "dev-daniel/ticket-skill/tk-hub": false, "dev-daniel/devboxes/tk-hub": false } });
    expect(store.view().counts).toEqual({ all: 7, attention: 0, active: 1, frontier: 1, blocked: 5, done: 0 });
    store.setFilter({ stages: ["blocked"] });
    expect(store.view().counts.all).toBe(7);
    expect(nns(store)).toEqual([["dev-daniel/ticket-board/tk-hub", ["07", "08", "09", "10", "22"]]]);
  });

  it("the devbox select keeps that box's boards and its counts", () => {
    const store = loaded();
    store.setFilter({ devbox: "dev-acme" });
    expect(store.view().boards).toEqual([]);
    expect(store.view().counts.all).toBe(0);
    store.setFilter({ devbox: "dev-daniel" });
    expect(store.view().boards).toHaveLength(3);
  });

  it("with no filter every board shows, in load order", () => {
    expect(nns(loaded()).map(([key]) => key)).toEqual([
      "dev-daniel/ticket-skill/tk-hub",
      "dev-daniel/devboxes/tk-hub",
      "dev-daniel/ticket-board/tk-hub",
    ]);
  });

  describe("applies /ws/ui events", () => {
    const ticket = (store: ReturnType<typeof createStore>, key: string, nn: string) =>
      store.view().boards.find((b) => b.key === key)?.tickets.find((t) => t.nn === nn);

    it("ticket_moved moves the ticket and marks it just-moved until cleared", () => {
      const store = loaded();
      const moved: TicketMoved = {
        ...fixture<TicketMoved>("ticket-moved"),
        board_key: "dev-daniel/ticket-board/tk-hub",
        nn: "27",
        from: "backlog",
        to: "working",
      };
      store.apply(moved);
      expect(ticket(store, "dev-daniel/ticket-board/tk-hub", "27")?.col).toBe("working");
      expect(store.view().counts).toMatchObject({ active: 3, frontier: 0 });
      expect(store.view().justMoved).toEqual(new Set([ticketId("dev-daniel/ticket-board/tk-hub", "27")]));
      store.clearMoved(ticketId("dev-daniel/ticket-board/tk-hub", "27"));
      expect(store.view().justMoved.size).toBe(0);
    });

    it("ticket_moved for a board it does not know is ignored", () => {
      const store = loaded();
      store.apply({ ...fixture<TicketMoved>("ticket-moved"), board_key: "x/y/z" });
      expect(store.view().justMoved.size).toBe(0);
    });

    it("board_synced replaces a known board in place and adds a new one at the end", () => {
      const store = loaded();
      store.apply(fixture("board-synced-stale"));
      const keys = store.view().boards.map((b) => b.key);
      expect(keys).toEqual([
        "dev-daniel/ticket-skill/tk-hub",
        "dev-daniel/devboxes/tk-hub",
        "dev-daniel/ticket-board/tk-hub",
      ]);
      const board = store.view().boards[2];
      expect(board.stale).toBe(true);
      expect(board.last_seen).toBe("2026-10-23T13:10:00Z");

      const other = fixture<{ board: Board }>("board-synced-stale").board;
      store.apply({ type: "board_synced", board: { ...other, key: "dev-acme/ticket-board/tk-hub", devbox: "dev-acme" } });
      expect(store.view().boards.map((b) => b.key).at(-1)).toBe("dev-acme/ticket-board/tk-hub");
    });

    it("board_removed drops the board", () => {
      const store = loaded();
      store.apply({ type: "board_removed", board_key: "dev-daniel/devboxes/tk-hub" });
      expect(store.view().boards.map((b) => b.key)).not.toContain("dev-daniel/devboxes/tk-hub");
      expect(store.view().counts.attention).toBe(2);
    });

    it("a board the user unticked stays unticked across board_synced; new boards follow the default", () => {
      const store = loaded();
      const hidden = "dev-daniel/devboxes/tk-hub";
      store.setFilter({ boards: { [hidden]: false } });
      const board = store.view().allBoards.find((b) => b.key === hidden)!;
      store.apply({ type: "board_synced", board });
      expect(store.view().boards.map((b) => b.key)).not.toContain(hidden);
      expect(store.view().counts.attention).toBe(2);
      const other = fixture<{ board: Board }>("board-synced-stale").board;
      store.apply({ type: "board_synced", board: { ...other, key: "dev-acme/ticket-board/tk-hub", devbox: "dev-acme" } });
      expect(store.view().boards.map((b) => b.key)).toContain("dev-acme/ticket-board/tk-hub");
    });

    it("an archived board is unticked by default; ticking it shows it, with its counts, and it stays archived", () => {
      const store = loaded();
      const board = store.view().allBoards[0];
      const archived = { ...board, hub_meta: { ...board.hub_meta, archived: true } };
      store.apply({ type: "board_synced", board: archived });
      expect(isBoardShown(store.view().filter, store.view().archivedBoards[0])).toBe(false);
      const before = store.view().counts.all;
      store.setFilter({ boards: { [board.key]: true } });
      expect(store.view().boards.map((b) => b.key)).toContain(board.key);
      expect(store.view().counts.all).toBe(before + board.tickets.length);
      expect(store.view().archivedBoards.map((b) => b.key)).toEqual([board.key]);
      store.apply({ type: "board_synced", board: archived });
      expect(store.view().boards.map((b) => b.key)).toContain(board.key);
    });

    it("an archived board leaves the views and counts and is listed in archivedBoards", () => {
      const store = loaded();
      const before = store.view().counts.all;
      const board = store.view().allBoards[0];
      store.apply({ type: "board_synced", board: { ...board, hub_meta: { ...board.hub_meta, archived: true } } });
      const view = store.view();
      expect(view.archivedBoards.map((b) => b.key)).toEqual([board.key]);
      expect(view.boards.map((b) => b.key)).not.toContain(board.key);
      expect(view.allBoards.map((b) => b.key)).not.toContain(board.key);
      expect(view.counts.all).toBe(before - board.tickets.length);
      store.apply({ type: "board_synced", board });
      expect(store.view().archivedBoards).toEqual([]);
      expect(store.view().counts.all).toBe(before);
    });

    it("devbox_status offline greys that box's boards with its last-seen, online clears it", () => {
      const store = loaded();
      store.apply({ ...fixture<DevboxStatus>("devbox-status-online"), online: false, health: null, last_seen: "2026-10-23T14:05:00Z" });
      expect(store.view().boards.every((b) => b.stale && b.last_seen === "2026-10-23T14:05:00Z")).toBe(true);
      expect(store.view().devboxes.find((d) => d.devbox === "dev-daniel")?.online).toBe(false);

      store.apply(fixture("devbox-status-online"));
      expect(store.view().boards.some((b) => b.stale)).toBe(false);
      expect(store.view().devboxes.find((d) => d.devbox === "dev-daniel")?.health?.herdr.ok).toBe(true);
    });

    it("devbox_status for a new box adds it to the devbox list", () => {
      const store = createStore();
      store.apply(fixture("devbox-status-refused"));
      expect(store.view().devboxes).toEqual([
        expect.objectContaining({ devbox: "dev-acme", refused_reason: expect.stringContaining("major differs") }),
      ]);
    });

    it("rpc_result raises a toast with the outcome", () => {
      const store = loaded();
      store.apply(fixture("rpc-result-ok"));
      store.apply(fixture("rpc-result-timeout"));
      expect(store.view().toasts.map((t) => [t.kind, t.text])).toEqual([
        ["ok", "launched tk-feat-27-hub-settings-page in tab w3:t6"],
        ["error", expect.any(String)],
      ]);
      store.dismiss(store.view().toasts[0].id);
      expect(store.view().toasts).toHaveLength(1);
    });
  });

  it("a launch timed out by the hub or daemon warns it may still launch; a failed launch stays an error", () => {
    const store = createStore();
    const launch = { type: "rpc_result", ok: false, exit_code: null, output_tail: "", devbox: "dev-daniel",
                     method: "launch_ticket", board_key: "a/b/c", nn: "27" } as const; // prettier-ignore
    store.apply({ ...launch, id: "r-1", reason: "timed out after 120s" });
    store.apply({ ...launch, id: "r-2", reason: "tk.sh launch timed out after 900s" });
    store.apply({ ...launch, id: "r-3", exit_code: 1, output_tail: "ERROR: launch failed", reason: "timed out after 120s" });
    store.apply({ ...launch, id: "r-4", method: "say_agent", reason: "timed out after 120s" });
    expect(store.view().toasts.map((t) => [t.kind, t.text])).toEqual([
      ["warn", "launch_ticket #27: still running on the box, may still launch; watch the board"],
      ["error", "launch_ticket: failed: tk.sh launch timed out after 900s"],
      ["error", "launch_ticket: failed: timed out after 120s"],
      ["error", "say_agent: failed: timed out after 120s"],
    ]);
  });

  it("notifies subscribers on every change and stops after unsubscribe", () => {
    const store = createStore();
    let calls = 0;
    const off = store.subscribe(() => calls++);
    store.load(boardsResponse());
    store.setFilter({ stages: ["active"] });
    off();
    store.apply(fixture("board-removed"));
    expect(calls).toBe(2);
  });

  it("pills are presets of the stage set; the pressed one is an exact match", () => {
    const store = loaded();
    expect(presetOf(store.view().filter.stages)).toBeNull();
    store.setFilter({ stages: stagesFor("all") });
    expect(store.view().filter.stages).toHaveLength(5);
    expect(presetOf(store.view().filter.stages)).toBe("all");
    store.setFilter({ stages: stagesFor("frontier") });
    expect(presetOf(store.view().filter.stages)).toBe("frontier");
    store.setFilter({ stages: ["frontier", "active"] });
    expect(presetOf(store.view().filter.stages)).toBeNull();
  });

  it("an opened ticket whose board is removed stays closed when the board returns", () => {
    const store = loaded();
    const board = store.view().allBoards[0];
    store.openTicket(board.key, board.tickets[0].nn);
    expect(store.view().sheet).not.toBeNull();
    store.apply({ type: "board_removed", board_key: board.key });
    expect(store.view().sheet).toBeNull();
    store.apply({ type: "board_synced", board });
    expect(store.view().sheet).toBeNull();
  });

  it("a tracked action whose rpc_result never arrives stops pending after the hub's timeout, with a warning", () => {
    vi.useFakeTimers();
    try {
      const store = createStore();
      store.track([{ id: "r-1", devbox: "dev-daniel", method: "launch_ticket", board_key: "a/b/c", nn: "27" }]);
      expect(PENDING_EXPIRY_MS).toBeGreaterThan(120_000); // the hub times an rpc out after 120 s
      vi.advanceTimersByTime(PENDING_EXPIRY_MS - 1);
      expect(store.view().pending.map((p) => p.id)).toEqual(["r-1"]);
      expect(store.view().toasts).toEqual([]);
      vi.advanceTimersByTime(1);
      expect(store.view().pending).toEqual([]);
      expect(store.view().toasts.map((t) => [t.kind, t.text])).toEqual([
        ["warn", "launch_ticket #27: no result came back; check the board"],
      ]);
    } finally {
      vi.useRealTimers();
    }
  });
});
