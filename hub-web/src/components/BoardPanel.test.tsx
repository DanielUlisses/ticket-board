import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { Board, SynthesisEvent } from "../protocol/types";
import { createStore } from "../store/store";
import { boardsResponse, fixture } from "../test/fixtures";
import { CardGrid } from "./CardGrid";
import { StoreProvider } from "./StoreContext";

const WITH_DOC = "dev-daniel/ticket-skill/tk-hub"; // board.md and an ok brief
const NO_DOC = "dev-daniel/devboxes/tk-hub"; // no board.md, no brief

function setup(key: string, patch?: (b: Board) => Board, refreshBrief = vi.fn().mockResolvedValue(true)) {
  const store = createStore();
  const res = boardsResponse();
  store.load({ ...res, boards: patch ? res.boards.map((b) => (b.key === key ? patch(b) : b)) : res.boards });
  const client = { setPolicy: vi.fn(), refreshBrief };
  render(
    <StoreProvider store={store}>
      <CardGrid client={client} />
    </StoreProvider>,
  );
  return { store, refreshBrief };
}

/** Opens the 📄 Board panel of the board with `key` (the nth region of the page). */
function openPanel(store: ReturnType<typeof createStore>, key: string) {
  const index = store.view().boards.findIndex((b) => b.key === key);
  const region = screen.getAllByRole("region")[index];
  fireEvent.click(within(region).getByRole("button", { name: /Board$/ }));
  return screen.getByRole("dialog", { name: "Board" });
}

describe("📄 Board panel", () => {
  it("shows board.md as sanitized markdown", () => {
    const { store } = setup(WITH_DOC, (b) => ({ ...b, board_md: "# Why\n\nOne plane.\n\n<script>alert(1)</script>" }));
    const panel = openPanel(store, WITH_DOC);
    expect(within(panel).getByRole("heading", { name: "Why" })).toBeInTheDocument();
    expect(within(panel).getByText("One plane.")).toBeInTheDocument();
    expect(panel.querySelector("script")).toBeNull();
  });

  it("says there is no board doc when the export has none", () => {
    const { store } = setup(NO_DOC);
    const panel = openPanel(store, NO_DOC);
    expect(within(panel).getByText(/no board doc/i)).toBeInTheDocument();
  });

  it("shows the brief's four lists with the backend and status that produced it", () => {
    const { store } = setup(WITH_DOC);
    const panel = openPanel(store, WITH_DOC);
    const section = (name: string) => within(panel).getByRole("group", { name });
    expect(section("Landed")).toHaveTextContent("04 /ticket writes board.md");
    expect(section("In flight")).toHaveTextContent("03 push (PR #41)");
    expect(section("Needs you")).toHaveTextContent("01 export: human review");
    expect(section("Next")).toHaveTextContent("Merge 03 once checks pass");
    expect(within(panel).getByTestId("brief-status")).toHaveTextContent("by agy");
  });

  it("says there is no brief yet when none was written", () => {
    const { store } = setup(NO_DOC);
    const panel = openPanel(store, NO_DOC);
    expect(within(panel).getByText(/no brief yet/i)).toBeInTheDocument();
  });

  it("↻ Refresh asks the hub for a new brief", () => {
    const { store, refreshBrief } = setup(WITH_DOC);
    const panel = openPanel(store, WITH_DOC);
    fireEvent.click(within(panel).getByRole("button", { name: /Refresh/ }));
    expect(refreshBrief).toHaveBeenCalledWith(WITH_DOC);
  });

  it("while a run works the old brief stays and Refresh is busy", () => {
    const { store } = setup(WITH_DOC);
    const panel = openPanel(store, WITH_DOC);
    act(() => store.apply({ ...fixture<SynthesisEvent>("synthesis-finished-board-brief"), type: "synthesis_started" }));
    expect(within(panel).getByTestId("brief-status")).toHaveTextContent("running");
    expect(within(panel).getByRole("group", { name: "Landed" })).toHaveTextContent("04 /ticket writes board.md");
    expect(within(panel).getByRole("button", { name: /Refresh/ })).toBeDisabled();
  });

  it("a failed run keeps the previous brief and shows the failure", () => {
    const { store } = setup(WITH_DOC);
    const panel = openPanel(store, WITH_DOC);
    act(() =>
      store.apply({
        ...fixture<SynthesisEvent>("synthesis-finished-board-brief"),
        type: "synthesis_failed",
        error: "agy: timed out after 300s",
      }),
    );
    expect(within(panel).getByTestId("brief-status")).toHaveTextContent("synthesis failed");
    expect(within(panel).getByText(/agy: timed out after 300s/)).toBeInTheDocument();
    expect(within(panel).getByRole("group", { name: "Landed" })).toHaveTextContent("04 /ticket writes board.md");
  });

  it("closes with the ✕ button", () => {
    const { store } = setup(WITH_DOC);
    const panel = openPanel(store, WITH_DOC);
    fireEvent.click(within(panel).getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog", { name: "Board" })).toBeNull();
  });
});
