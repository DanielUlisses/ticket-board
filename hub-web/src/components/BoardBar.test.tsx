import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Board } from "../protocol/types";
import { createStore } from "../store/store";
import { boardsResponse } from "../test/fixtures";
import { CardGrid } from "./CardGrid";
import { StoreProvider } from "./StoreContext";

const KEY = "dev-daniel/ticket-board/tk-hub";

function setup(setPolicy = vi.fn().mockResolvedValue(true)) {
  const client = { setPolicy };
  const store = createStore();
  store.load(boardsResponse());
  render(
    <StoreProvider store={store}>
      <CardGrid client={client} />
    </StoreProvider>,
  );
  const bar = screen.getByRole("region", { name: /ticket-board on dev-daniel/ });
  return { client, store, bar };
}

const open = (bar: HTMLElement) => {
  fireEvent.click(within(bar).getByRole("button", { name: "Board policy" }));
  return screen.getByRole("dialog", { name: "Board policy" });
};

describe("BoardBar policy", () => {
  it("tags the bar with autonomy and forge", () => {
    const { bar } = setup();
    expect(within(bar).getByTitle("autonomy · forge")).toHaveTextContent("auto · github");
  });

  it("shows each value with the layer it comes from", () => {
    const { bar } = setup();
    const pop = open(bar);
    const row = (name: string) => within(pop).getByRole("group", { name });
    expect(within(row("autonomy")).getByRole("combobox")).toHaveValue("auto");
    expect(row("autonomy")).toHaveTextContent("default");
    expect(row("forge")).toHaveTextContent("detected");
    expect(within(row("night_shift")).getByRole("checkbox")).not.toBeChecked();
    expect(row("max_parallel")).toHaveTextContent("5");
    expect(row("max_parallel")).toHaveTextContent("box");
  });

  it("keeps the box-level values read-only", () => {
    const { bar } = setup();
    const pop = open(bar);
    for (const name of ["max_parallel", "usage_gate_pct"]) {
      const row = within(pop).getByRole("group", { name });
      expect(within(row).queryByRole("combobox")).toBeNull();
      expect(within(row).queryByRole("checkbox")).toBeNull();
      expect(within(row).queryByRole("textbox")).toBeNull();
      expect(within(row).queryByRole("spinbutton")).toBeNull();
    }
  });

  it("writes an edit to the board override and follows the next snapshot", () => {
    const { client, store, bar } = setup();
    const pop = open(bar);
    fireEvent.change(within(within(pop).getByRole("group", { name: "autonomy" })).getByRole("combobox"), {
      target: { value: "assist" },
    });
    fireEvent.click(within(within(pop).getByRole("group", { name: "auto_merge" })).getByRole("checkbox"));
    expect(client.setPolicy).toHaveBeenNthCalledWith(1, KEY, { autonomy: "assist" });
    expect(client.setPolicy).toHaveBeenNthCalledWith(2, KEY, { auto_merge: true });

    // The daemon re-exports after set_policy: the new snapshot reaches the popover and the tag.
    const board = store.view().boards.find((b) => b.key === KEY)!;
    act(() =>
      store.apply({
        type: "board_synced",
        board: {
          ...board,
          policy: { ...board.policy, autonomy: "assist" },
          policy_sources: { ...board.policy_sources, autonomy: "board" },
        },
      }),
    );
    const row = within(screen.getByRole("dialog", { name: "Board policy" })).getByRole("group", { name: "autonomy" });
    expect(within(row).getByRole("combobox")).toHaveValue("assist");
    expect(row).toHaveTextContent("board");
    expect(within(bar).getByTitle("autonomy · forge")).toHaveTextContent("assist · github");
  });

  it("closes on Escape", () => {
    const { bar } = setup();
    open(bar);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "Board policy" })).toBeNull();
  });

  const group = (pop: HTMLElement, name: string) => within(pop).getByRole("group", { name });
  const sync = (store: ReturnType<typeof createStore>, patch: (b: Board) => Board) =>
    act(() => store.apply({ type: "board_synced", board: patch(store.view().boards.find((b) => b.key === KEY)!) }));

  describe("no remote", () => {
    it("warns on the forge row and disables the select", () => {
      const { store, bar } = setup();
      sync(store, (b) => ({ ...b, policy_sources: { ...b.policy_sources, forge: "no-remote" } }));
      const pop = open(bar);
      expect(group(pop, "forge")).toHaveTextContent("repo has no remote — forge is forced to none");
      expect(within(group(pop, "forge")).getByRole("combobox")).toBeDisabled();
    });
  });

  describe("inherit", () => {
    const withBoardOverride = (store: ReturnType<typeof createStore>) =>
      sync(store, (b) => ({ ...b, policy_sources: { ...b.policy_sources, autonomy: "board" } }));

    it("offers a reset only on rows whose source is board, and sends null", () => {
      const { client, store, bar } = setup();
      const pop = open(bar);
      expect(within(pop).queryByRole("button", { name: "inherit autonomy" })).toBeNull();
      withBoardOverride(store);
      fireEvent.click(within(pop).getByRole("button", { name: "inherit autonomy" }));
      expect(client.setPolicy).toHaveBeenCalledWith(KEY, { autonomy: null });
    });

    it("stays pending until the source is no longer board", () => {
      const { store, bar } = setup();
      withBoardOverride(store);
      const pop = open(bar);
      fireEvent.click(within(pop).getByRole("button", { name: "inherit autonomy" }));
      expect(group(pop, "autonomy")).toHaveTextContent("saving…");
      expect(within(group(pop, "autonomy")).getByRole("combobox")).toBeDisabled();
      sync(store, (b) => ({ ...b, policy_sources: { ...b.policy_sources, autonomy: "repo" } }));
      expect(group(pop, "autonomy")).not.toHaveTextContent("saving…");
      expect(within(group(pop, "autonomy")).getByRole("combobox")).toBeEnabled();
    });
  });

  describe("pending edit", () => {
    afterEach(() => vi.useRealTimers());

    it("shows the new value as saving and locks the input until a snapshot reflects it", () => {
      const { store, bar } = setup(vi.fn().mockReturnValue(new Promise(() => {})));
      const pop = open(bar);
      fireEvent.change(within(group(pop, "autonomy")).getByRole("combobox"), { target: { value: "assist" } });
      const select = within(group(pop, "autonomy")).getByRole("combobox");
      expect(select).toHaveValue("assist");
      expect(select).toBeDisabled();
      expect(group(pop, "autonomy")).toHaveTextContent("saving…");

      // A snapshot that does not carry the change yet leaves it pending.
      sync(store, (b) => ({ ...b }));
      expect(group(pop, "autonomy")).toHaveTextContent("saving…");

      sync(store, (b) => ({
        ...b,
        policy: { ...b.policy, autonomy: "assist" },
        policy_sources: { ...b.policy_sources, autonomy: "board" },
      }));
      expect(group(pop, "autonomy")).not.toHaveTextContent("saving…");
      expect(within(group(pop, "autonomy")).getByRole("combobox")).toBeEnabled();
    });

    it("clears when the save is rejected", async () => {
      const { bar } = setup(vi.fn().mockResolvedValue(false));
      const pop = open(bar);
      fireEvent.click(within(group(pop, "night_shift")).getByRole("checkbox"));
      await act(async () => {});
      const box = within(group(pop, "night_shift")).getByRole("checkbox");
      expect(box).toBeEnabled();
      expect(box).not.toBeChecked();
      expect(group(pop, "night_shift")).not.toHaveTextContent("saving…");
    });

    it("gives up waiting for a snapshot after a while", () => {
      vi.useFakeTimers();
      const { bar } = setup(vi.fn().mockReturnValue(new Promise(() => {})));
      const pop = open(bar);
      fireEvent.click(within(group(pop, "night_shift")).getByRole("checkbox"));
      expect(group(pop, "night_shift")).toHaveTextContent("saving…");
      act(() => void vi.advanceTimersByTime(20_000));
      expect(group(pop, "night_shift")).not.toHaveTextContent("saving…");
    });
  });
});
