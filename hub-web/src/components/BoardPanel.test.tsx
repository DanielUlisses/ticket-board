import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { Board, SynthesisEvent } from "../protocol/types";
import { createStore, stagesFor } from "../store/store";
import { boardsResponse, fixture } from "../test/fixtures";
import { CardGrid } from "./CardGrid";
import { StoreProvider } from "./StoreContext";

const WITH_DOC = "dev-daniel/ticket-skill/tk-hub"; // board.md and an ok brief
const NO_DOC = "dev-daniel/devboxes/tk-hub"; // no board.md, no brief

function setup(
  key: string,
  patch?: (b: Board) => Board,
  refreshBrief = vi.fn().mockResolvedValue(true),
  extra: Record<string, unknown> = {},
) {
  const store = createStore();
  const res = boardsResponse();
  store.load({ ...res, boards: patch ? res.boards.map((b) => (b.key === key ? patch(b) : b)) : res.boards });
  const client = { setPolicy: vi.fn(), refreshBrief, ...extra };
  store.setFilter({ stages: stagesFor("all") }); // a board whose tickets are all done is hidden by the default stage filter
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

const allDone = (b: Board): Board => ({ ...b, tickets: b.tickets.map((t) => ({ ...t, col: "done" as const })) });
const PROPOSALS = "### M1 Keep rules short\n\nWrite one line per rule.\n\n### E1 Trim the skill\n\nDrop step 3.\n";

describe("Retro and sweep", () => {
  const retroApi = (over: Record<string, unknown> = {}) => ({
    runRetro: vi.fn().mockResolvedValue({ proposals_md: PROPOSALS, results_md: null }),
    applyRetro: vi.fn().mockResolvedValue({
      proposals_md: PROPOSALS.replace("### M1 Keep rules short\n", "### M1 Keep rules short\n**Mark:** approved\n"),
      results_md: "# Retro results\n\nM1 approved",
    }),
    sweepBoard: vi.fn().mockResolvedValue({
      removed: [{ kind: "worktree", ref: "/w/alpha--fix-01" }],
      skipped: [{ kind: "branch", ref: "fix-02", reason: "dirty" }],
    }),
    proposeMemory: vi.fn().mockResolvedValue(true),
    ...over,
  });

  it("Retro is disabled until every ticket is done", () => {
    const { store } = setup(WITH_DOC, undefined, undefined, retroApi());
    const panel = openPanel(store, WITH_DOC);
    expect(within(panel).getByRole("button", { name: /Retro/ })).toBeDisabled();
  });

  it("lists the saved proposals from the board's data without running Retro", () => {
    const { store } = setup(WITH_DOC, (b) => ({ ...allDone(b), retro_proposals_md: PROPOSALS }), undefined, retroApi());
    const panel = openPanel(store, WITH_DOC);
    expect(within(panel).getByRole("group", { name: "M1" })).toHaveTextContent("Keep rules short");
    expect(within(panel).getByRole("group", { name: "E1" })).toHaveTextContent("Trim the skill");
  });

  it("mark buttons are disabled while a mark is being applied", async () => {
    let finish: (v: unknown) => void = () => {};
    const api = retroApi({ applyRetro: vi.fn(() => new Promise((r) => (finish = r))) });
    const { store } = setup(WITH_DOC, (b) => ({ ...allDone(b), retro_proposals_md: PROPOSALS }), undefined, api);
    const panel = openPanel(store, WITH_DOC);
    const m1 = within(panel).getByRole("group", { name: "M1" });
    fireEvent.click(within(m1).getByRole("button", { name: "Approve" }));
    expect(within(m1).getByRole("button", { name: "Reject" })).toBeDisabled();
    await act(async () => finish({ proposals_md: PROPOSALS, results_md: null }));
    expect(within(m1).getByRole("button", { name: "Reject" })).toBeEnabled();
  });

  it("Retro runs and lists the proposals by id", async () => {
    const api = retroApi();
    const { store } = setup(WITH_DOC, allDone, undefined, api);
    const panel = openPanel(store, WITH_DOC);
    fireEvent.click(within(panel).getByRole("button", { name: /Retro/ }));
    expect(await within(panel).findByRole("group", { name: "M1" })).toHaveTextContent("Keep rules short");
    expect(within(panel).getByRole("group", { name: "E1" })).toHaveTextContent("Trim the skill");
    expect(api.runRetro).toHaveBeenCalledWith(WITH_DOC);
  });

  it("approve / reject / defer call retro-apply and show the regenerated results", async () => {
    const api = retroApi();
    const { store } = setup(WITH_DOC, allDone, undefined, api);
    const panel = openPanel(store, WITH_DOC);
    fireEvent.click(within(panel).getByRole("button", { name: /Retro/ }));
    const m1 = await within(panel).findByRole("group", { name: "M1" });
    fireEvent.click(within(m1).getByRole("button", { name: "Approve" }));
    expect(api.applyRetro).toHaveBeenCalledWith(WITH_DOC, "approve", ["M1"]);
    expect(await within(panel).findByText(/M1 approved/)).toBeInTheDocument();
    expect(within(panel.querySelector('[aria-label="M1"]') as HTMLElement).getByText("approved")).toBeInTheDocument();
    fireEvent.click(within(panel.querySelector('[aria-label="E1"]') as HTMLElement).getByRole("button", { name: "Defer" }));
    expect(api.applyRetro).toHaveBeenLastCalledWith(WITH_DOC, "defer", ["E1"]);
    await waitFor(() => expect(within(panel.querySelector('[aria-label="E1"]') as HTMLElement).getByRole("button", { name: "Reject" })).toBeEnabled());
    fireEvent.click(within(panel.querySelector('[aria-label="E1"]') as HTMLElement).getByRole("button", { name: "Reject" }));
    expect(api.applyRetro).toHaveBeenLastCalledWith(WITH_DOC, "reject", ["E1"]);
  });

  it("→ memory ticket prefills the memory field from an M item only", async () => {
    const api = retroApi();
    const { store } = setup(WITH_DOC, allDone, undefined, api);
    const panel = openPanel(store, WITH_DOC);
    fireEvent.click(within(panel).getByRole("button", { name: /Retro/ }));
    const m1 = await within(panel).findByRole("group", { name: "M1" });
    expect(within(within(panel).getByRole("group", { name: "E1" })).queryByRole("button", { name: /memory ticket/ })).toBeNull();
    fireEvent.click(within(m1).getByRole("button", { name: /memory ticket/ }));
    const field = within(panel).getByRole("textbox", { name: /memory/i });
    expect(field).toHaveValue("Keep rules short\n\nWrite one line per rule.");
    fireEvent.click(within(panel).getByRole("button", { name: /Propose memory/ }));
    expect(api.proposeMemory).toHaveBeenCalledWith(WITH_DOC, "Keep rules short\n\nWrite one line per rule.");
    await waitFor(() => expect(field).toHaveValue(""));
  });

  it("Sweep is disabled until a retro has results", () => {
    const { store } = setup(WITH_DOC, (b) => ({ ...allDone(b), retro_results_md: null }), undefined, retroApi());
    const panel = openPanel(store, WITH_DOC);
    expect(within(panel).getByRole("button", { name: /Sweep/ })).toBeDisabled();
  });

  it("Sweep lists what it removed and what it skipped", async () => {
    const api = retroApi();
    const { store } = setup(WITH_DOC, (b) => ({ ...allDone(b), retro_results_md: "# Retro results" }), undefined, api);
    const panel = openPanel(store, WITH_DOC);
    fireEvent.click(within(panel).getByRole("button", { name: /Sweep/ }));
    expect(await within(panel).findByText(/alpha--fix-01/)).toBeInTheDocument();
    expect(api.sweepBoard).toHaveBeenCalledWith(WITH_DOC);
    expect(within(panel).getByText(/fix-02/)).toHaveTextContent("dirty");
  });
});
