import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { BoardSynced, ReportResponse, TicketMoved } from "../protocol/types";
import { createStore } from "../store/store";
import { boardsResponse, fixture } from "../test/fixtures";
import { CardGrid } from "./CardGrid";
import { LiveBoard } from "./LiveBoard";
import { StoreProvider } from "./StoreContext";
import { TicketSheet, type DetailApi } from "./TicketSheet";

const KEY = "dev-daniel/ticket-skill/tk-hub";

const REPORT: ReportResponse = {
  board_key: KEY,
  nn: "01",
  title: "Ticket 01",
  full_report_md: "## Coordinator report\n\n```sh\ntk.sh export tk-hub\n```",
  body: "# Ticket body\n\nDo the thing.\n\n```py\nprint('hi')\n```",
};

function setup(open: [string, string] | null = [KEY, "01"], api: Partial<DetailApi> = {}) {
  const client: DetailApi = {
    activity: vi.fn().mockResolvedValue([
      { from: "backlog", to: "working", at: "2026-10-23T14:02:00Z" },
      { from: "working", to: "review", at: "2026-10-23T15:40:00Z" },
    ]),
    report: vi.fn().mockResolvedValue(REPORT),
    launch: vi.fn().mockResolvedValue(undefined),
    say: vi.fn().mockResolvedValue(null),
    paneTail: vi.fn().mockResolvedValue(undefined),
    ...api,
  };
  const store = createStore();
  store.load(boardsResponse());
  if (open) store.openTicket(open[0], open[1]);
  render(
    <StoreProvider store={store}>
      <CardGrid />
      <LiveBoard />
      <TicketSheet client={client} />
    </StoreProvider>,
  );
  return { store, client };
}

const sheet = () => screen.getByRole("dialog", { name: /^#01 / });

describe("TicketSheet", () => {
  it("is closed until a ticket is opened", () => {
    setup(null);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  // A ticket's card (article) and, on a live board, its puck (div) share one accessible name.
  const card = (nn: string) =>
    screen.getAllByRole("button", { name: new RegExp(`^${nn} `) }).find((e) => e.tagName === "ARTICLE")!;
  const puck = (nn: string) =>
    screen.getAllByRole("button", { name: new RegExp(`^${nn} `) }).find((e) => e.tagName === "DIV")!;

  it("opens from a card and from a puck", () => {
    setup(null);
    fireEvent.click(card("01"));
    expect(sheet()).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    fireEvent.click(puck("05"));
    expect(screen.getByRole("dialog", { name: /^#05 / })).toBeInTheDocument();
  });

  it("Enter opens the sheet from a card and from a puck", () => {
    setup(null);
    fireEvent.keyDown(card("01"), { key: "Enter" });
    expect(sheet()).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    fireEvent.keyDown(puck("05"), { key: "Enter" });
    expect(screen.getByRole("dialog", { name: /^#05 / })).toBeInTheDocument();
  });

  it("closes on the close button, the backdrop and Esc", () => {
    const { store } = setup();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    act(() => store.openTicket(KEY, "01"));
    fireEvent.click(screen.getByTestId("sheet-backdrop"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    act(() => store.openTicket(KEY, "01"));
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("header names the glyph, stage, devbox and board", () => {
    setup();
    const header = within(sheet()).getByRole("banner");
    expect(within(header).getByTitle("HUMAN GATE")).toHaveTextContent("◉");
    expect(within(header).getByText("dev-daniel")).toBeInTheDocument();
    expect(within(header).getByText("tk-hub")).toBeInTheDocument();
  });

  it("renders the synthesis card", () => {
    setup();
    const card = within(sheet()).getByRole("region", { name: "Synthesis" });
    expect(within(card).getByText(/^Adds the JSON export verbs/)).toBeInTheDocument();
    expect(within(card).getByText("Check stdout stays pure JSON when the forge errors.")).toBeInTheDocument();
    expect(within(card).getByText("feat-01-tk-sh-boards---tk-sh-exp")).toBeInTheDocument();
    expect(within(card).getByText("agy · ok")).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "⤢ Full Review & Reasoning" })).toBeInTheDocument();
  });

  it("renders the checklist with done state", () => {
    setup();
    const list = within(sheet()).getByRole("region", { name: "Manual Validation Checklist" });
    const boxes = within(list).getAllByRole("checkbox");
    expect(boxes.map((b) => (b as HTMLInputElement).checked)).toEqual([true, false, false]);
    expect(within(list).getByText("Check an unknown board exits non-zero")).toBeInTheDocument();
  });

  it("renders project memory read-only", () => {
    setup();
    const mem = within(sheet()).getByRole("region", { name: "Project Memory" });
    expect(within(mem).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "`tk.sh` verbs run with the repo's main checkout as cwd, never a worktree.",
      "Shell scripts stay shellcheck-clean; logs go to stderr, data to stdout.",
    ]);
  });

  it("renders the activity timeline from the endpoint", async () => {
    const { client } = setup();
    expect(await screen.findByText("backlog → working 14:02Z")).toBeInTheDocument();
    expect(screen.getByText("→ review 15:40Z")).toBeInTheDocument();
    expect(client.activity).toHaveBeenCalledWith(KEY, "01");
  });

  it("has an agent pane", () => {
    setup();
    expect(within(sheet()).getByRole("region", { name: "Agent pane" })).toBeInTheDocument();
  });

  it("updates when its ticket changes in the store", async () => {
    const { store, client } = setup();
    await waitFor(() => expect(client.activity).toHaveBeenCalledTimes(1));
    act(() => store.apply({ ...fixture<TicketMoved>("ticket-moved"), to: "done" }));
    await waitFor(() => expect(client.activity).toHaveBeenCalledTimes(2));
    expect(within(sheet()).getByText("DONE")).toBeInTheDocument();
    const synced = fixture<BoardSynced>("board-synced-stale");
    const board = boardsResponse().boards.find((b) => b.key === KEY)!;
    act(() =>
      store.apply({
        type: "board_synced",
        board: {
          ...synced.board,
          ...board,
          tickets: board.tickets.map((t) => (t.nn === "01" ? { ...t, summary: "Fresh summary." } : t)),
        },
      }),
    );
    expect(within(sheet()).getByText("Fresh summary.")).toBeInTheDocument();
  });

  it("an injected script or HTML in any field renders inert", () => {
    const store = createStore();
    const data = boardsResponse();
    const evil = `<script>window.__pwned=1</script><img src=x onerror="window.__pwned=1">`;
    const board = data.boards.find((b) => b.key === KEY)!;
    board.project_memory = `1. ${evil}`;
    board.tickets[0] = {
      ...board.tickets[0],
      summary: evil,
      reviewer_notes: evil,
      manual_tasks: [{ text: evil, done: false, source: "synthesis" }],
    };
    store.load(data);
    store.openTicket(KEY, "01");
    const { container } = render(
      <StoreProvider store={store}>
        <TicketSheet
          client={{
            activity: async () => [],
            report: async () => REPORT,
            launch: async () => {},
            say: async () => null,
            paneTail: async () => {},
          }}
        />
      </StoreProvider>,
    );
    expect(container.ownerDocument.querySelector("script, img[onerror]")).toBeNull();
    expect(screen.getAllByText(evil).length).toBeGreaterThan(0);
  });
});

describe("report modal", () => {
  async function openReport() {
    const ctx = setup();
    fireEvent.click(within(sheet()).getByRole("button", { name: "⤢ Full Review & Reasoning" }));
    await screen.findByRole("dialog", { name: "Full Review & Reasoning" });
    return ctx;
  }
  const modal = () => screen.getByRole("dialog", { name: "Full Review & Reasoning" });

  it("renders full_report_md and the ticket body as markdown", async () => {
    const { client } = await openReport();
    expect(client.report).toHaveBeenCalledWith(KEY, "01");
    expect(await within(modal()).findByRole("heading", { name: "Coordinator report" })).toBeInTheDocument();
    expect(within(modal()).getByRole("heading", { name: "Ticket body" })).toBeInTheDocument();
  });

  it("copy buttons put a code block's text on the clipboard", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    await openReport();
    const buttons = await within(modal()).findAllByRole("button", { name: "Copy" });
    expect(buttons).toHaveLength(2);
    fireEvent.click(buttons[0]);
    expect(writeText).toHaveBeenCalledWith("tk.sh export tk-hub\n");
    fireEvent.click(buttons[1]);
    expect(writeText).toHaveBeenLastCalledWith("print('hi')\n");
  });

  it("strips script and HTML injection from both documents", async () => {
    const evil = `<script>window.__pwned=1</script><img src=x onerror="window.__pwned=1"><a href="javascript:alert(1)">x</a>`;
    setup([KEY, "01"], { report: async () => ({ ...REPORT, full_report_md: `# R\n\n${evil}`, body: `# B\n\n${evil}` }) });
    fireEvent.click(within(sheet()).getByRole("button", { name: "⤢ Full Review & Reasoning" }));
    const m = await screen.findByRole("dialog", { name: "Full Review & Reasoning" });
    await within(m).findByRole("heading", { name: "R" });
    expect(m.querySelector("script, img[onerror]")).toBeNull();
    expect(m.querySelector('a[href^="javascript"]')).toBeNull();
  });

  it("says so when the hub has no ticket body (rest_report body null)", async () => {
    setup([KEY, "01"], { report: async () => ({ ...REPORT, full_report_md: "# R", body: null }) });
    fireEvent.click(within(sheet()).getByRole("button", { name: "⤢ Full Review & Reasoning" }));
    const m = await screen.findByRole("dialog", { name: "Full Review & Reasoning" });
    await within(m).findByRole("heading", { name: "R" });
    expect(within(within(m).getByRole("region", { name: "Ticket body" })).getByText("No ticket body.")).toBeInTheDocument();
  });

  it("closes on its backdrop and leaves the sheet open", async () => {
    await openReport();
    fireEvent.click(modal().parentElement!);
    expect(screen.queryByRole("dialog", { name: "Full Review & Reasoning" })).not.toBeInTheDocument();
    expect(sheet()).toBeInTheDocument();
  });

  it("closes on Esc without closing the sheet", async () => {
    await openReport();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "Full Review & Reasoning" })).not.toBeInTheDocument();
    expect(sheet()).toBeInTheDocument();
  });
});
