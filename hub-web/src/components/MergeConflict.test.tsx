import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { MergeConflict, Pr, Ticket } from "../protocol/types";
import { createStore } from "../store/store";
import { boardsResponse } from "../test/fixtures";
import { Card } from "./Card";
import { StoreProvider } from "./StoreContext";
import { TicketSheet, type DetailApi } from "./TicketSheet";

const GITHUB = "dev-daniel/ticket-board/tk-hub";
const PR: Pr = { number: 7, isDraft: false, mergeable: "CONFLICTING", review: "APPROVED", checks: [] };

const conflict = (state: MergeConflict["state"], patch: Partial<MergeConflict> = {}): MergeConflict => ({
  state,
  started_at: "2026-10-23T14:02:10Z",
  finished_at: state === "resolving" ? null : "2026-10-23T14:09:45Z",
  base: "main",
  files: ["src/a.py", "README.md"],
  summary: null,
  reason: null,
  output_tail: null,
  ...patch,
});

function setup(mc: MergeConflict | null) {
  const client: DetailApi = {
    activity: vi.fn().mockResolvedValue([]),
    diff: vi.fn().mockResolvedValue({ base: "abc", diff_hash: "sha256:1", files: [] }),
    report: vi.fn().mockResolvedValue({ full_report_md: null, body: "" }),
    launch: vi.fn().mockResolvedValue(undefined),
    proposeMemory: vi.fn().mockResolvedValue(true),
    say: vi.fn().mockResolvedValue("r-say"),
    paneTail: vi.fn().mockResolvedValue(undefined),
    ship: vi.fn().mockResolvedValue(true),
    resolveNoCode: vi.fn().mockResolvedValue(true),
    refine: vi.fn().mockResolvedValue(true),
    setTaskDone: vi.fn().mockResolvedValue(true),
    finishConflict: vi.fn().mockResolvedValue(true),
    abortConflict: vi.fn().mockResolvedValue(true),
  };
  const data = boardsResponse();
  const board = data.boards.find((b) => b.key === GITHUB)!;
  const ticket = board.tickets[0]!;
  Object.assign(ticket, { col: "pr", pr: PR, worktree: "/w/t", merge_conflict: mc } satisfies Partial<Ticket>);
  const store = createStore();
  store.load(data);
  store.openTicket(GITHUB, ticket.nn);
  render(
    <StoreProvider store={store}>
      <Card ticket={ticket} boardKey={GITHUB} />
      <TicketSheet client={client} />
    </StoreProvider>,
  );
  return { client, ticket };
}

const sheet = () => screen.getByRole("dialog");
const footer = () => screen.getByRole("contentinfo", { name: "Actions" });
const section = () => within(sheet()).getByRole("region", { name: "Merge conflict" });
const card = (t: Ticket) => screen.getByRole("button", { name: new RegExp(`^${t.nn} `) });

afterEach(cleanup);

describe("merge conflict on the sheet", () => {
  it("resolving: the card and sheet say so, and a second ✓ Merge can't be clicked", () => {
    const { ticket } = setup(conflict("resolving"));
    expect(within(card(ticket)).getByText("resolving conflict")).toBeInTheDocument();
    expect(within(section()).getByText(/ticket-merger is resolving the conflict with main/)).toBeInTheDocument();
    expect(within(footer()).getByRole("button", { name: /Merge/ })).toBeDisabled();
    expect(within(footer()).queryByRole("button", { name: /Commit merge/ })).toBeNull();
  });

  it("review: the merger's summary, the conflicted files and the merge's diff against the branch's head", async () => {
    const { client, ticket } = setup(conflict("review", { summary: "src/a.py: **kept both** sides" }));
    expect(within(card(ticket)).getByText("merge resolved — review")).toBeInTheDocument();
    const s = section();
    expect(within(s).getByText("Merge resolved — review")).toBeInTheDocument();
    expect(within(s).getByText("kept both")).toBeInTheDocument();
    expect(within(s).getByText("README.md")).toBeInTheDocument();
    await waitFor(() => expect(client.diff).toHaveBeenCalledWith(GITHUB, ticket.nn, undefined, "head"));
    expect(within(footer()).queryByRole("button", { name: "✓ Merge" })).toBeNull();
  });

  it("Commit merge commits and pushes only after the explicit confirm", () => {
    const { client, ticket } = setup(conflict("review"));
    fireEvent.click(within(footer()).getByRole("button", { name: "Commit merge" }));
    expect(client.finishConflict).not.toHaveBeenCalled();
    expect(within(footer()).getByText(/commits the merge and pushes it/)).toBeInTheDocument();
    fireEvent.click(within(footer()).getByRole("button", { name: "Cancel" }));
    expect(client.finishConflict).not.toHaveBeenCalled();
    fireEvent.click(within(footer()).getByRole("button", { name: "Commit merge" }));
    fireEvent.click(within(footer()).getByRole("button", { name: "Commit & push" }));
    expect(client.finishConflict).toHaveBeenCalledWith(GITHUB, ticket.nn);
  });

  it("Abort sends abort_conflict", () => {
    const { client, ticket } = setup(conflict("review"));
    fireEvent.click(within(footer()).getByRole("button", { name: "Abort merge" }));
    expect(client.abortConflict).toHaveBeenCalledWith(GITHUB, ticket.nn);
    expect(client.finishConflict).not.toHaveBeenCalled();
  });

  it("failed: says why with the agent's output tail, and ✓ Merge is back", () => {
    const { ticket } = setup(
      conflict("failed", {
        reason: "ticket-merger timed out after 600s; the merge was aborted",
        output_tail: "API error: overloaded",
      }),
    );
    expect(within(card(ticket)).getByText("conflict — resolver failed")).toBeInTheDocument();
    expect(within(section()).getByText(/timed out after 600s/)).toBeInTheDocument();
    expect(within(section()).getByText("API error: overloaded")).toBeInTheDocument();
    expect(within(footer()).getByRole("button", { name: "✓ Merge" })).toBeEnabled();
    expect(within(footer()).queryByRole("button", { name: "Commit merge" })).toBeNull();
  });

  it("failed: Abort merge is still offered, for a resolve whose own abort failed", () => {
    const { client, ticket } = setup(conflict("failed", { reason: "interrupted: abort failed (exit 1)" }));
    fireEvent.click(within(footer()).getByRole("button", { name: "Abort merge" }));
    expect(client.abortConflict).toHaveBeenCalledWith(GITHUB, ticket.nn);
  });

  it("no conflict: no section and no tag", () => {
    const { ticket } = setup(null);
    expect(within(sheet()).queryByRole("region", { name: "Merge conflict" })).toBeNull();
    expect(within(card(ticket)).queryByText(/conflict|merge resolved/)).toBeNull();
  });
});
