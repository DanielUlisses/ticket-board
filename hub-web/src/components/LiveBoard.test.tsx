import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { TicketMoved } from "../protocol/types";
import { createStore } from "../store/store";
import { boardsResponse, fixture } from "../test/fixtures";
import { LiveBoard } from "./LiveBoard";
import { StoreProvider } from "./StoreContext";

function renderBoard() {
  const store = createStore();
  store.load(boardsResponse());
  render(
    <StoreProvider store={store}>
      <LiveBoard />
    </StoreProvider>,
  );
  return store;
}

const lane = (repo: string) => screen.getByRole("row", { name: new RegExp(`^tk-hub ${repo}`) });
const cellOf = (row: HTMLElement, nn: string) =>
  within(row).getByTitle(new RegExp(`^${nn} `)).closest("td")!;
const headers = () => screen.getAllByRole("columnheader").map((h) => h.textContent);

describe("LiveBoard", () => {
  it("is an 8-column swimlane under a Live Stage Swimlanes toolbar, a header per column", () => {
    renderBoard();
    expect(screen.getByText("Live Stage Swimlanes")).toBeInTheDocument();
    expect(headers()).toEqual([
      "BOARD / DEVBOX",
      "!NEEDS YOU",
      "○FRONTIER",
      "⊘BLOCKED",
      "◐WORKING",
      "◑REVIEW",
      "◉HUMAN GATE",
      "⇡PR / PUSHED",
      "✓DONE",
    ]);
  });

  it("a puck shows #NN, model, title and checks", () => {
    renderBoard();
    const puck = within(lane("ticket-skill")).getByTitle(/^01 /);
    expect(within(puck).getByText("#01")).toBeInTheDocument();
    expect(within(puck).getByText("sonnet")).toBeInTheDocument();
    expect(within(puck).getByText("tk.sh boards + tk.sh export as JSON")).toBeInTheDocument();
    expect(within(puck).getByText("tests pass · 5/5 · rev ✓")).toBeInTheDocument();
  });

  it("has a lane per board with each ticket's puck in its column", () => {
    renderBoard();
    const row = lane("ticket-skill");
    const cells = within(row).getAllByRole("cell");
    expect(cells).toHaveLength(8);
    expect(cellOf(row, "01")).toBe(cells[5]); // human
    expect(cellOf(row, "03")).toBe(cells[6]); // pr
    expect(within(lane("devboxes")).getByText("dev-daniel").closest("[data-health]")).toHaveAttribute("data-health", "ok");
  });

  it("plays pulseMove on a ticket that just moved, once", () => {
    const store = renderBoard();
    act(() =>
      store.apply({
        ...fixture<TicketMoved>("ticket-moved"),
        board_key: "dev-daniel/ticket-board/tk-hub",
        nn: "27",
        from: "backlog",
        to: "working",
      }),
    );
    const row = lane("ticket-board");
    const puck = within(row).getByTitle(/^27 /);
    expect(cellOf(row, "27")).toBe(within(row).getAllByRole("cell")[3]);
    expect(puck).toHaveAttribute("data-moved", "true");
    expect(within(row).getByTitle(/^06 /)).toHaveAttribute("data-moved", "false");

    fireEvent.animationEnd(puck);
    expect(within(row).getByTitle(/^27 /)).toHaveAttribute("data-moved", "false");
  });

  it("follows the store's filters and greys stale lanes", () => {
    const store = renderBoard();
    act(() => {
      store.apply(fixture("board-synced-stale"));
      store.setFilter({ stages: ["attention"] });
    });
    expect(screen.getAllByRole("row")).toHaveLength(3); // header + two lanes
    act(() => store.setFilter({ stages: ["attention", "active", "frontier", "blocked", "done"] }));
    expect(lane("ticket-board")).toHaveAttribute("data-stale", "true");
  });
});
