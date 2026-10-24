import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Pr, RpcResult, Ticket } from "../protocol/types";
import { createStore } from "../store/store";
import { boardsResponse } from "../test/fixtures";
import { StoreProvider } from "./StoreContext";
import { TicketSheet, type DetailApi } from "./TicketSheet";

const GITHUB = "dev-daniel/ticket-board/tk-hub";
const NO_FORGE = "dev-daniel/devboxes/tk-hub";

const PR: Pr = {
  number: 7,
  isDraft: false,
  mergeable: "MERGEABLE",
  review: "APPROVED",
  checks: [{ n: "build", s: "bad" }],
};

const HASH = "sha256:" + "b".repeat(64);
const APPROVED = { approved: true, diff_hash: HASH, at: "2026-10-23T10:00:00Z" };

function setup(boardKey: string, patch: Partial<Ticket>) {
  const client: DetailApi = {
    activity: vi.fn().mockResolvedValue([]),
    diff: vi.fn().mockResolvedValue({ files: [], diff_hash: HASH }),
    report: vi.fn().mockResolvedValue({ full_report_md: null, body: "" }),
    launch: vi.fn().mockResolvedValue(undefined),
    proposeMemory: vi.fn().mockResolvedValue(true),
    say: vi.fn().mockResolvedValue("r-say"),
    paneTail: vi.fn().mockResolvedValue(undefined),
    ship: vi.fn().mockResolvedValue(true),
    resolveNoCode: vi.fn().mockResolvedValue(true),
    setReviewComments: vi.fn().mockResolvedValue(true),
    approveReview: vi.fn().mockResolvedValue(true),
    requestChanges: vi.fn().mockResolvedValue(true),
    refine: vi.fn().mockResolvedValue(true),
    setTaskDone: vi.fn().mockResolvedValue(true),
  };
  const data = boardsResponse();
  const board = data.boards.find((b) => b.key === boardKey)!;
  const ticket = board.tickets[0]!;
  Object.assign(ticket, patch);
  const store = createStore();
  store.load(data);
  store.openTicket(boardKey, ticket.nn);
  render(
    <StoreProvider store={store}>
      <TicketSheet client={client} />
    </StoreProvider>,
  );
  return { store, client, board, ticket };
}

const footer = () => screen.getByRole("contentinfo", { name: "Actions" });
const buttons = () => within(footer()).queryAllByRole("button").map((b) => b.textContent);

afterEach(cleanup);

describe("ship footer", () => {
  it("human on a PR forge: ⇡ Open PR is primary", () => {
    const { client, board, ticket } = setup(GITHUB, { col: "human" });
    expect(buttons()).toContain("⇡ Open PR");
    expect(buttons()).not.toContain("⇡ Push via SSH");
    fireEvent.click(within(footer()).getByRole("button", { name: /Open PR/ }));
    expect(client.ship).toHaveBeenCalledWith("⇡ Open PR", board.key, ticket.nn, "open_pr");
  });

  it("forge=none offers ⇡ Push via SSH and never a PR", async () => {
    const { client, board, ticket } = setup(NO_FORGE, { col: "human", human_review: APPROVED });
    expect(board.policy.forge).toBe("none");
    expect(buttons().join(" ")).not.toMatch(/Open PR|Merge/);
    const push = within(footer()).getByRole("button", { name: /Push via SSH/ });
    await waitFor(() => expect(push).toBeEnabled());
    fireEvent.click(push);
    expect(client.ship).toHaveBeenCalledWith("⇡ Push via SSH", board.key, ticket.nn, "ssh_push");
  });

  it("require_human_before_pr: ship is disabled without an approval of the current diff", async () => {
    const { client, board } = setup(NO_FORGE, { col: "human" });
    expect(board.policy.require_human_before_pr).toBe(true);
    const push = within(footer()).getByRole("button", { name: /Push via SSH/ });
    expect(push).toBeDisabled();
    expect(await within(footer()).findByText(/approve the diff in the Review tab/i)).toBeInTheDocument();
    fireEvent.click(push);
    expect(client.ship).not.toHaveBeenCalled();
  });

  it("require_human_before_pr: an approval of an older diff is void, and says so", async () => {
    setup(NO_FORGE, { col: "human", human_review: { ...APPROVED, diff_hash: "sha256:old" } });
    expect(await within(footer()).findByText(/changed since approval/)).toBeInTheDocument();
    expect(within(footer()).getByRole("button", { name: /Push via SSH/ })).toBeDisabled();
  });

  it("without require_human_before_pr, review is optional: ship needs no approval or diff", () => {
    const { client, board } = setup(GITHUB, { col: "human" });
    expect(board.policy.require_human_before_pr).toBe(false);
    expect(within(footer()).getByRole("button", { name: /Open PR/ })).toBeEnabled();
    expect(client.diff).not.toHaveBeenCalled();
  });

  it("pr: ✓ Merge and Verify, with the PR's gates", () => {
    const { client, board, ticket } = setup(GITHUB, { col: "pr", pr: PR });
    const gates = within(footer()).getByRole("list", { name: "Merge gates" });
    expect(within(gates).getByText(/checks — failed: build/)).toHaveAttribute("data-status", "fail");
    expect(within(gates).getByText(/mergeable/)).toHaveAttribute("data-status", "pass");
    fireEvent.click(within(footer()).getByRole("button", { name: "✓ Merge" }));
    fireEvent.click(within(footer()).getByRole("button", { name: "Verify" }));
    expect(client.ship).toHaveBeenCalledWith("✓ Merge", board.key, ticket.nn, "merge_pr");
    expect(client.ship).toHaveBeenCalledWith("Verify", board.key, ticket.nn, "verify_landed");
  });

  it("a merge refused by gates shows the gates it reported", () => {
    const { store, board, ticket } = setup(GITHUB, { col: "pr", pr: { ...PR, checks: [] } });
    const result: RpcResult = {
      type: "rpc_result", id: "r-9", ok: false, exit_code: 11, output_tail: "", method: "merge_pr",
      board_key: board.key, nn: ticket.nn, reason: "checks pending",
      data: { gates: [{ gate: "pr", status: "pass", detail: "#7" }, { gate: "checks", status: "fail", detail: "still running: build" }] },
    };
    act(() => store.apply(result));
    const gates = within(footer()).getByRole("list", { name: "Merge gates" });
    expect(within(gates).getAllByRole("listitem")).toHaveLength(2);
    expect(within(gates).getByText(/still running: build/)).toHaveAttribute("data-status", "fail");
  });

  it("verify shows the landed verdict and the open PR", () => {
    const { store, board, ticket } = setup(GITHUB, { col: "pr", pr: PR });
    act(() =>
      store.apply({
        type: "rpc_result", id: "r-8", ok: true, exit_code: 0, output_tail: "", method: "verify_landed",
        board_key: board.key, nn: ticket.nn, data: { landed: "no", open_pr: "#7 open" },
      }),
    );
    expect(within(footer()).getByLabelText("Verify result")).toHaveTextContent("Landed: no · Open PR: #7 open");
  });

  it("verify shows a JSON open PR as #<number>", () => {
    const { store, board, ticket } = setup(GITHUB, { col: "pr", pr: PR });
    act(() =>
      store.apply({
        type: "rpc_result", id: "r-9", ok: true, exit_code: 0, output_tail: "", method: "verify_landed",
        board_key: board.key, nn: ticket.nn, data: { landed: "no", open_pr: '{"number":42,"state":"OPEN"}' },
      }),
    );
    expect(within(footer()).getByLabelText("Verify result")).toHaveTextContent("Landed: no · Open PR: #42");
  });

  it("done and landed: Resolve is one click, a plain resolve with no reason", () => {
    const { client, board, ticket } = setup(GITHUB, { col: "done", landed: "yes:ancestry" });
    expect(within(footer()).queryByLabelText("Reason (one line)")).toBeNull();
    fireEvent.click(within(footer()).getByRole("button", { name: "Resolve" }));
    expect(client.ship).toHaveBeenCalledWith("Resolve", board.key, ticket.nn, "resolve_ticket");
    expect(client.resolveNoCode).not.toHaveBeenCalled();
  });

  it("human offers Resolve without code, which needs a reason", () => {
    const { client, board, ticket } = setup(GITHUB, { col: "human" });
    fireEvent.click(within(footer()).getByRole("button", { name: "Resolve without code" }));
    const resolve = within(footer()).getByRole("button", { name: "Resolve" });
    expect(resolve).toBeDisabled();
    fireEvent.change(within(footer()).getByLabelText("Reason (one line)"), { target: { value: "  notes only " } });
    fireEvent.click(resolve);
    expect(client.resolveNoCode).toHaveBeenCalledWith(board.key, ticket.nn, "  notes only ");
  });

  it("backlog and blocked cannot be resolved without code", () => {
    setup(GITHUB, { col: "backlog" });
    expect(buttons().join(" ")).not.toMatch(/Resolve/);
    cleanup();
    setup(GITHUB, { col: "blocked" });
    expect(buttons().join(" ")).not.toMatch(/Resolve/);
  });

  it("pr on a forge=none board offers no Merge or Verify", () => {
    setup(NO_FORGE, { col: "pr", pr: PR });
    expect(buttons().join(" ")).not.toMatch(/Merge|Verify/);
  });

  it("a merge that went through but whose resolve failed says so", () => {
    const { store, board, ticket } = setup(GITHUB, { col: "pr", pr: { ...PR, checks: [] } });
    act(() =>
      store.apply({
        type: "rpc_result", id: "r-7", ok: false, exit_code: 1, output_tail: "", method: "merge_pr",
        board_key: board.key, nn: ticket.nn, reason: "merged, but resolve failed: boom",
        data: { gates: [], merged: true },
      }),
    );
    expect(within(footer()).getByRole("alert")).toHaveTextContent("merged, but resolve failed: boom");
  });

  it("a new PR state in a snapshot drops the kept merge result, so the live gates show again", () => {
    const { store, board, ticket } = setup(GITHUB, { col: "pr", pr: { ...PR, checks: [] } });
    act(() =>
      store.apply({
        type: "rpc_result", id: "r-6", ok: false, exit_code: 12, output_tail: "", method: "merge_pr",
        board_key: board.key, nn: ticket.nn, reason: "gate failed",
        data: { gates: [{ gate: "checks", status: "fail", detail: "failed: build" }] },
      }),
    );
    expect(within(footer()).getAllByRole("listitem")).toHaveLength(1);
    const next = structuredClone(board);
    next.tickets[0]!.pr = { ...PR, checks: [] , number: 8 };
    act(() => store.apply({ type: "board_synced", board: next }));
    expect(within(footer()).getAllByRole("listitem")).toHaveLength(5);
  });

  it("done and not landed offers nothing; working offers nothing", () => {
    setup(GITHUB, { col: "done", landed: "no" });
    expect(buttons()).toEqual([]);
    cleanup();
    setup(GITHUB, { col: "working" });
    expect(buttons().join(" ")).not.toMatch(/Resolve|Merge|PR|Push/);
  });
});
