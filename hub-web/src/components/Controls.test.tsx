import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { createStore } from "../store/store";
import { boardsResponse } from "../test/fixtures";
import { CardGrid } from "./CardGrid";
import { Controls } from "./Controls";
import { LiveBoard } from "./LiveBoard";
import { StoreProvider } from "./StoreContext";

function setup(stale = false, page: "cards" | "live" = "cards") {
  const store = createStore();
  const data = boardsResponse();
  const archived = { ...data.boards[0], hub_meta: { archived: true }, stale };
  store.load({ ...data, boards: [archived, ...data.boards.slice(1)] });
  const onArchive = vi.fn();
  render(
    <StoreProvider store={store}>
      <Controls onArchive={onArchive} />
      {page === "cards" ? <CardGrid /> : <LiveBoard />}
    </StoreProvider>,
  );
  return { store, archived, onArchive };
}

const open = (name: string) => fireEvent.click(screen.getByRole("button", { name }));
const cards = () =>
  screen.queryAllByRole("button").filter((e) => e.tagName === "ARTICLE").map((a) => a.getAttribute("aria-label")!.split(" ")[0]);

describe("Controls Stage multi-select", () => {
  it("summarises the default: every group but Done", () => {
    setup();
    expect(screen.getByRole("button", { name: "Stage" })).toHaveTextContent("Stages (4)");
    open("Stage");
    expect(screen.getAllByRole("checkbox").map((c) => (c as HTMLInputElement).checked)).toEqual([true, true, true, true, false]);
  });

  it("ticking Done shows finished tickets; All and None are shortcuts", () => {
    const { store, archived } = setup();
    act(() => store.setFilter({ boards: { [archived.key]: true } })); // ticket 04 lives on that board
    expect(cards()).not.toContain("04");
    open("Stage");
    fireEvent.click(screen.getByRole("checkbox", { name: "Done" }));
    expect(cards()).toContain("04");
    fireEvent.click(screen.getByRole("button", { name: "No stages" }));
    expect(screen.getByRole("button", { name: "Stage" })).toHaveTextContent("Stages (0)");
    fireEvent.click(screen.getByRole("button", { name: "All stages" }));
    expect(screen.getByRole("button", { name: "Stage" })).toHaveTextContent("Stages (5)");
  });

  it("closes on Escape", () => {
    setup();
    open("Stage");
    fireEvent.keyDown(screen.getByRole("group", { name: "Stage filter" }), { key: "Escape" });
    expect(screen.queryByRole("group", { name: "Stage filter" })).not.toBeInTheDocument();
  });

  it("drives the Live Board too", () => {
    const { store, archived } = setup(false, "live");
    act(() => store.setFilter({ boards: { [archived.key]: true } }));
    expect(screen.queryByTitle(/^04 /)).not.toBeInTheDocument();
    open("Stage");
    fireEvent.click(screen.getByRole("checkbox", { name: "Done" }));
    expect(screen.getByTitle(/^04 /)).toBeInTheDocument();
  });
});

describe("Controls Board multi-select", () => {
  it("lists archived boards unticked in an Archived section; ticking shows one without calling the RPC", () => {
    const { archived, onArchive } = setup();
    expect(screen.getByRole("button", { name: "Board" })).toHaveTextContent("Boards (2)");
    open("Board");
    expect(screen.getByText("Archived (1)")).toBeInTheDocument();
    const box = screen.getByRole("checkbox", { name: /ticket-skill.*archived/ });
    expect(box).not.toBeChecked();
    fireEvent.click(box);
    expect(box).toBeChecked();
    expect(onArchive).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Board" })).toHaveTextContent("Boards (3)");
    expect(cards()).toContain("01");
    expect(archived.hub_meta.archived).toBe(true);
  });

  it("unticking a board hides it from Cards", () => {
    setup();
    expect(cards()).toContain("07");
    open("Board");
    fireEvent.click(screen.getByRole("checkbox", { name: /ticket-board/ }));
    expect(cards()).not.toContain("07");
  });

  it("Live Board follows the Board ticks", () => {
    setup(false, "live");
    expect(screen.getByTitle(/^07 /)).toBeInTheDocument();
    open("Board");
    fireEvent.click(screen.getByRole("checkbox", { name: /ticket-board/ }));
    expect(screen.queryByTitle(/^07 /)).not.toBeInTheDocument();
  });

  it("Restore and Archive are explicit buttons on the row", () => {
    const { archived, onArchive } = setup();
    open("Board");
    fireEvent.click(screen.getByRole("button", { name: /^Restore tk-hub · ticket-skill/ }));
    expect(onArchive).toHaveBeenCalledWith(archived.key, false);
    const live = screen.getByRole("button", { name: /^Archive tk-hub · ticket-board/ });
    fireEvent.click(live);
    expect(onArchive).toHaveBeenLastCalledWith("dev-daniel/ticket-board/tk-hub", true);
  });

  it("disables Archive and Restore for boards on an offline box", () => {
    const store = createStore();
    const [first, second] = boardsResponse().boards;
    store.load({
      ...boardsResponse(),
      boards: [
        { ...first, hub_meta: { archived: true }, stale: true },
        { ...second, stale: true },
      ],
    });
    render(
      <StoreProvider store={store}>
        <Controls onArchive={vi.fn()} />
      </StoreProvider>,
    );
    open("Board");
    expect(screen.getByRole("button", { name: /^Restore/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /^Archive/ })).toBeDisabled();
  });
});

describe("Controls Board picks vs archiving", () => {
  const archiveFlow = () => {
    const ctx = setup();
    const { store } = ctx;
    const flip = (key: string, archived: boolean) =>
      act(() => {
        const b = store.view().archivedBoards.concat(store.view().allBoards).find((x) => x.key === key)!;
        store.apply({ type: "board_synced", board: { ...b, hub_meta: { ...b.hub_meta, archived } } });
      });
    return { ...ctx, flip };
  };

  it("All then Archive: the board is no longer shown nor counted", () => {
    const { flip } = archiveFlow();
    open("Board");
    fireEvent.click(screen.getByRole("button", { name: "All boards" }));
    expect(cards()).toContain("07");
    fireEvent.click(screen.getByRole("button", { name: /^Archive tk-hub · ticket-board/ }));
    flip("dev-daniel/ticket-board/tk-hub", true);
    expect(cards()).not.toContain("07");
    expect(screen.getByRole("button", { name: "Board" })).toHaveTextContent("Boards (1)");
  });

  it("None then Restore: the board is shown again", () => {
    const { archived, flip } = archiveFlow();
    open("Board");
    fireEvent.click(screen.getByRole("button", { name: "No boards" }));
    fireEvent.click(screen.getByRole("button", { name: /^Restore tk-hub · ticket-skill/ }));
    flip(archived.key, false);
    expect(cards()).toContain("01");
  });

  it("clicking Archive or Restore drops the board's pick", () => {
    const { store, archived } = archiveFlow();
    open("Board");
    fireEvent.click(screen.getByRole("button", { name: "All boards" }));
    fireEvent.click(screen.getByRole("button", { name: /^Archive tk-hub · ticket-board/ }));
    expect("dev-daniel/ticket-board/tk-hub" in store.view().filter.boards).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: /^Restore tk-hub · ticket-skill/ }));
    expect(archived.key in store.view().filter.boards).toBe(false);
  });

  it("All ticks only the live boards; archived ones stay explicit", () => {
    const { archived } = setup();
    open("Board");
    fireEvent.click(screen.getByRole("button", { name: "All boards" }));
    expect(screen.getByRole("checkbox", { name: /ticket-skill.*archived/ })).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Board" })).toHaveTextContent("Boards (2)");
    expect(archived.hub_meta.archived).toBe(true);
  });
});

describe("Controls dropdown", () => {
  it("has no aria-haspopup", () => {
    setup();
    expect(screen.getByRole("button", { name: "Board" })).not.toHaveAttribute("aria-haspopup");
  });

  it("closes when focus leaves it, stays open when focus moves inside", () => {
    setup();
    open("Board");
    const group = screen.getByRole("group", { name: "Board filter" });
    const inside = within(group).getAllByRole("checkbox")[0];
    fireEvent.blur(screen.getByRole("button", { name: "Board" }), { relatedTarget: inside });
    expect(screen.getByRole("group", { name: "Board filter" })).toBeInTheDocument();
    fireEvent.blur(inside, { relatedTarget: screen.getByLabelText("Devbox") });
    expect(screen.queryByRole("group", { name: "Board filter" })).not.toBeInTheDocument();
  });

  it("stays open when a blur has no relatedTarget (clicks inside the panel in some browsers)", () => {
    setup();
    open("Board");
    const inside = within(screen.getByRole("group", { name: "Board filter" })).getAllByRole("checkbox")[0];
    fireEvent.blur(inside, { relatedTarget: null });
    expect(screen.getByRole("group", { name: "Board filter" })).toBeInTheDocument();
  });
});

describe("archived boards that are ticked", () => {
  it.each(["cards", "live"] as const)("are marked and labelled archived on the %s page", (page) => {
    const { store, archived } = setup(false, page);
    act(() => store.setFilter({ boards: { [archived.key]: true } }));
    const el = screen.getByLabelText(/tk-hub ticket-skill/);
    expect(el).toHaveAttribute("data-archived", "true");
    expect(within(el).getByText("archived")).toBeInTheDocument();
    const live = screen.getByLabelText(/tk-hub ticket-board/);
    expect(live).toHaveAttribute("data-archived", "false");
    expect(within(live).queryByText("archived")).not.toBeInTheDocument();
  });
});

describe("Controls pills", () => {
  it("set the Stage selection; the pressed one is an exact match", () => {
    setup();
    const pill = (n: RegExp) => screen.getByRole("button", { name: n });
    expect(screen.queryAllByRole("button", { pressed: true })).toHaveLength(0);
    fireEvent.click(pill(/^Frontier/));
    expect(pill(/^Frontier/)).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Stage" })).toHaveTextContent("Stages (1)");
    fireEvent.click(pill(/^All$/));
    expect(pill(/^All$/)).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Stage" })).toHaveTextContent("Stages (5)");
    open("Stage");
    fireEvent.click(within(screen.getByRole("group", { name: "Stage filter" })).getByRole("checkbox", { name: "Done" }));
    expect(pill(/^All$/)).toHaveAttribute("aria-pressed", "false");
  });
});
