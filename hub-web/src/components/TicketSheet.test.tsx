import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { BoardSynced, ReportResponse, SynthesisEvent, TicketMoved } from "../protocol/types";
import { createStore } from "../store/store";
import { boardsResponse, fixture } from "../test/fixtures";
import { CardGrid } from "./CardGrid";
import { LiveBoard } from "./LiveBoard";
import { StoreProvider } from "./StoreContext";
import { TICK_CONFIRM_MS, TicketSheet, type DetailApi } from "./TicketSheet";

const KEY = "dev-daniel/ticket-skill/tk-hub";

const REPORT: ReportResponse = {
  board_key: KEY,
  nn: "01",
  title: "Ticket 01",
  full_report_md: "## Coordinator report\n\n```sh\ntk.sh export tk-hub\n```",
  body: "# Ticket body\n\nDo the thing.\n\n```py\nprint('hi')\n```",
};

function stubClient(): DetailApi {
  return {
    activity: async () => [],
    diff: (async () => ({ files: [] })) as unknown as DetailApi["diff"],
    report: async () => REPORT,
    launch: async () => {},
    say: async () => null,
    paneTail: async () => {},
    proposeMemory: async () => true,
    ship: async () => true,
    resolveNoCode: async () => true,
    refine: async () => true,
    setTaskDone: async () => true,
    setReviewComments: async () => true,
    approveReview: async () => true,
    requestChanges: async () => true,
  };
}

function setup(open: [string, string] | null = [KEY, "01"], api: Partial<DetailApi> = {}) {
  const client: DetailApi = {
    activity: vi.fn().mockResolvedValue([
      { from: "backlog", to: "working", at: "2026-10-23T14:02:00Z" },
      { from: "working", to: "review", at: "2026-10-23T15:40:00Z" },
    ]),
    diff: vi.fn().mockResolvedValue({ files: [{ path: "a.ts", status: "M", added: 1, removed: 0, binary: false }] }),
    report: vi.fn().mockResolvedValue(REPORT),
    refine: vi.fn().mockResolvedValue(true),
    setTaskDone: vi.fn().mockResolvedValue(true),
    proposeMemory: vi.fn().mockResolvedValue(true),
    launch: vi.fn().mockResolvedValue(undefined),
    say: vi.fn().mockResolvedValue(null),
    paneTail: vi.fn().mockResolvedValue(undefined),
    ship: vi.fn().mockResolvedValue(true),
    resolveNoCode: vi.fn().mockResolvedValue(true),
    setReviewComments: vi.fn().mockResolvedValue(true),
    approveReview: vi.fn().mockResolvedValue(true),
    requestChanges: vi.fn().mockResolvedValue(true),
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

  it("has a Review tab on a ticket with a worktree, loading the diff only when it is opened", async () => {
    const { client } = setup([KEY, "01"]);
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
    expect(within(sheet()).getByRole("region", { name: "Synthesis" })).toBeInTheDocument();
    expect(client.diff).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("tab", { name: "Review" }));
    expect(await screen.findByRole("button", { name: /a\.ts/ })).toBeInTheDocument();
    expect(client.diff).toHaveBeenCalledWith(KEY, "01");
    expect(within(sheet()).queryByRole("region", { name: "Synthesis" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Overview" }));
    expect(within(sheet()).getByRole("region", { name: "Synthesis" })).toBeInTheDocument();
  });

  it("follows the WAI-ARIA tabs pattern: linked ids, roving tabindex, arrow/Home/End keys", () => {
    setup([KEY, "01"]);
    const overview = screen.getByRole("tab", { name: "Overview" });
    const review = screen.getByRole("tab", { name: "Review" });
    expect(overview).toHaveAttribute("tabindex", "0");
    expect(review).toHaveAttribute("tabindex", "-1");
    const panel = screen.getByRole("tabpanel");
    expect(panel).toHaveAttribute("id", overview.getAttribute("aria-controls"));
    expect(panel).toHaveAttribute("aria-labelledby", overview.id);
    expect(review).not.toHaveAttribute("aria-controls"); // its panel is not rendered

    fireEvent.keyDown(overview, { key: "ArrowRight" });
    expect(review).toHaveAttribute("aria-selected", "true");
    expect(review).toHaveFocus();
    expect(review).toHaveAttribute("tabindex", "0");
    expect(overview).toHaveAttribute("tabindex", "-1");
    expect(screen.getByRole("tabpanel")).toHaveAttribute("aria-labelledby", review.id);
    expect(screen.getByRole("tabpanel")).toHaveAttribute("id", review.getAttribute("aria-controls"));
    expect(overview).not.toHaveAttribute("aria-controls");

    fireEvent.keyDown(review, { key: "ArrowRight" }); // wraps
    expect(overview).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(overview, { key: "ArrowLeft" }); // wraps back
    expect(review).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(review, { key: "Home" });
    expect(overview).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(overview, { key: "End" });
    expect(review).toHaveAttribute("aria-selected", "true");
  });

  it("has no Review tab on a ticket without a worktree", () => {
    setup([KEY, "04"]);
    expect(screen.queryByRole("tab")).not.toBeInTheDocument();
  });

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
      "tk.sh verbs run with the repo's main checkout as cwd, never a worktree.",
      "Shell scripts stay shellcheck-clean; logs go to stderr, data to stdout.",
    ]);
  });

  it("renders the activity timeline from the endpoint", async () => {
    const { client } = setup();
    expect(await screen.findByText("backlog → working 14:02Z")).toBeInTheDocument();
    expect(screen.getByText("→ review 15:40Z")).toBeInTheDocument();
    expect(client.activity).toHaveBeenCalledWith(KEY, "01");
  });

  it("shows a Request changes round in the timeline among the moves", async () => {
    setup([KEY, "01"], {
      activity: vi.fn().mockResolvedValue([
        { type: "ticket_moved", from: "working", to: "review", at: "2026-10-23T15:40:00Z" },
        { type: "changes_requested", comments: 2, at: "2026-10-23T15:52:00Z" },
        { type: "ticket_moved", from: "review", to: "working", at: "2026-10-23T15:53:00Z" },
      ]),
    });
    expect(await screen.findByText("working → review 15:40Z")).toBeInTheDocument();
    expect(screen.getByText("changes requested (2 comments) 15:52Z")).toBeInTheDocument();
    expect(screen.getByText("→ working 15:53Z")).toBeInTheDocument();
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
    const evil = `visible-marker <script>window.__pwned=1</script><img src=x onerror="window.__pwned=1"><a href="x" onclick="window.__pwned=1">c</a> [x](javascript:window.__pwned=1)`;
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
          client={stubClient()}
        />
      </StoreProvider>,
    );
    expect(container.ownerDocument.querySelector("script, iframe, [onerror], [onclick]")).toBeNull();
    expect(container.ownerDocument.querySelector('a[href^="javascript:"]')).toBeNull();
    // the fields still rendered, as inert text
    expect(within(sheet()).queryAllByRole("link")).toHaveLength(0);
    expect(sheet().textContent).toContain("visible-marker");
  });

  it("keeps paragraphs and single newlines in summary and notes", () => {
    const store = createStore();
    const data = boardsResponse();
    data.boards.find((b) => b.key === KEY)!.tickets[0] = {
      ...data.boards.find((b) => b.key === KEY)!.tickets[0],
      summary: "First para.\n\nSecond para.",
      reviewer_notes: "line one\nline two",
    };
    store.load(data);
    store.openTicket(KEY, "01");
    render(
      <StoreProvider store={store}>
        <TicketSheet client={stubClient()} />
      </StoreProvider>,
    );
    const first = within(sheet()).getByText("First para.");
    expect(first.tagName).toBe("P");
    expect(within(sheet()).getByText("Second para.").tagName).toBe("P");
    expect(first).not.toBe(within(sheet()).getByText("Second para."));
    const notes = within(sheet()).getByText(/line one/);
    expect(notes.tagName).toBe("P");
    expect(notes.textContent).toBe("line one\nline two"); // CSS (.compact p) keeps the break visible
  });

  it("renders Markdown in summary, notes, checklist and memory (code, no literal backticks)", () => {
    const store = createStore();
    const data = boardsResponse();
    const board = data.boards.find((b) => b.key === KEY)!;
    board.project_memory = "- run `bin/devbox-config` first";
    board.tickets[0] = {
      ...board.tickets[0],
      summary: "Use `$DEVBOX_CLIENTS` here",
      reviewer_notes: "Check `jq .a` and **bold**",
      manual_tasks: [
        { text: "Run `make test`", done: true, source: "synthesis" },
        { text: "Open [docs](https://example.com)", done: false, source: "synthesis" },
      ],
    };
    store.load(data);
    store.openTicket(KEY, "01");
    render(
      <StoreProvider store={store}>
        <TicketSheet client={stubClient()} />
      </StoreProvider>,
    );
    const s = sheet();
    for (const code of ["bin/devbox-config", "$DEVBOX_CLIENTS", "jq .a", "make test"]) {
      expect(within(s).getByText(code).tagName).toBe("CODE");
    }
    expect(s.textContent).not.toContain("`");
    expect(within(s).getByText("bold").tagName).toBe("STRONG");
    expect(within(s).getByRole("link", { name: "docs" })).toHaveAttribute("href", "https://example.com");
    // ticking state still mirrors the data, and the label still names the checkbox
    const boxes = within(within(s).getByRole("region", { name: "Manual Validation Checklist" })).getAllByRole("checkbox");
    const checklist = within(s).getByRole("region", { name: "Manual Validation Checklist" });
    expect(within(checklist).getByRole("checkbox", { name: "Run make test" })).toBeChecked();
    expect(within(checklist).getByRole("checkbox", { name: "Open docs" })).not.toBeChecked();
    expect(boxes.length).toBe(2);
  });
});

describe("checklist and refine", () => {
  const checklist = () => within(sheet()).getByRole("region", { name: "Manual Validation Checklist" });
  const box = (name: string) => within(checklist()).getByRole("checkbox", { name }) as HTMLInputElement;
  const UNKNOWN = "Check an unknown board exits non-zero";
  const started = (): SynthesisEvent => fixture<SynthesisEvent>("synthesis-started");

  it("a tick goes to the hub and shows at once", async () => {
    const { client } = setup();
    fireEvent.click(box(UNKNOWN));
    expect(client.setTaskDone).toHaveBeenCalledWith(KEY, "01", UNKNOWN, true);
    expect(box(UNKNOWN).checked).toBe(true);
    fireEvent.click(box("Run tk.sh export against a real board and pipe through jq -e"));
    expect(client.setTaskDone).toHaveBeenLastCalledWith(
      KEY,
      "01",
      "Run tk.sh export against a real board and pipe through jq -e",
      false,
    );
  });

  it("a tick the hub refuses is undone", async () => {
    setup([KEY, "01"], { setTaskDone: vi.fn().mockResolvedValue(false) });
    fireEvent.click(box(UNKNOWN));
    await waitFor(() => expect(box(UNKNOWN).checked).toBe(false));
  });

  it("a tick the box never confirms is undone after TICK_CONFIRM_MS", async () => {
    vi.useFakeTimers();
    try {
      setup();
      fireEvent.click(box(UNKNOWN));
      expect(box(UNKNOWN).checked).toBe(true);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(TICK_CONFIRM_MS);
      });
      expect(box(UNKNOWN).checked).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it("✦ Update via agy is disabled while a run is running", () => {
    const { store } = setup();
    const card = within(sheet()).getByRole("region", { name: "Synthesis" });
    act(() => store.apply(started()));
    expect(within(card).getByRole("button", { name: "✦ Update via agy" })).toBeDisabled();
  });

  it("marks developer items", () => {
    setup();
    const item = box("Try it on the home-assistant board too").closest("li")!;
    expect(within(item).getByText("you")).toBeInTheDocument();
  });

  it("✦ Update via agy sends the comment and spins until the run finishes", async () => {
    const { store, client } = setup();
    const card = within(sheet()).getByRole("region", { name: "Synthesis" });
    expect(within(card).queryByRole("status", { name: "Synthesis running" })).not.toBeInTheDocument();
    fireEvent.change(within(card).getByRole("textbox", { name: "Comment for agy" }), {
      target: { value: "Shorter, please." },
    });
    fireEvent.click(within(card).getByRole("button", { name: "✦ Update via agy" }));
    expect(client.refine).toHaveBeenCalledWith(KEY, "01", "Shorter, please.");
    expect(within(card).getByRole("status", { name: "Synthesis running" })).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "✦ Update via agy" })).toBeDisabled();
    await waitFor(() => expect(within(card).getByRole("textbox", { name: "Comment for agy" })).toHaveValue(""));
    act(() => store.apply(started()));
    expect(within(card).getByText("agy · running")).toBeInTheDocument();
    act(() => store.apply({ ...started(), type: "synthesis_finished" }));
    expect(within(card).queryByRole("status", { name: "Synthesis running" })).not.toBeInTheDocument();
    expect(within(card).getByText("agy · ok")).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "✦ Update via agy" })).toBeEnabled();
  });

  it("a refine the hub refuses stops the spinner and keeps the comment", async () => {
    setup([KEY, "01"], { refine: vi.fn().mockResolvedValue(false) });
    const card = within(sheet()).getByRole("region", { name: "Synthesis" });
    fireEvent.change(within(card).getByRole("textbox", { name: "Comment for agy" }), { target: { value: "x" } });
    fireEvent.click(within(card).getByRole("button", { name: "✦ Update via agy" }));
    await waitFor(() =>
      expect(within(card).queryByRole("status", { name: "Synthesis running" })).not.toBeInTheDocument(),
    );
    expect(within(card).getByRole("textbox", { name: "Comment for agy" })).toHaveValue("x");
  });

  it("a failed run shows its reason under the backend label", () => {
    const { store } = setup();
    act(() =>
      store.apply({ ...fixture<SynthesisEvent>("synthesis-failed"), board_key: KEY, nn: "01", error: "agy: invalid output" }),
    );
    const card = within(sheet()).getByRole("region", { name: "Synthesis" });
    expect(within(card).getByText("agy · failed")).toBeInTheDocument();
    expect(within(card).getByText("agy: invalid output")).toBeInTheDocument();
  });

  it("shows the run's new_memory_rule as a suggestion", () => {
    setup();
    const mem = within(sheet()).getByRole("region", { name: "Project Memory" });
    expect(within(mem).getByLabelText("Suggested memory rule")).toHaveTextContent(
      "Export verbs print JSON only on stdout; everything else goes to stderr.",
    );
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
    const evil = `visible-marker <script>window.__pwned=1</script><img src=x onerror="window.__pwned=1"><a href="javascript:alert(1)">x</a>`;
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

  describe("Propose to memory", () => {
    const dialog = () => screen.getByRole("dialog");
    const field = () => within(dialog()).getByRole("textbox", { name: "Propose to memory" }) as HTMLTextAreaElement;
    const button = () => within(dialog()).getByRole("button", { name: "Propose to memory" });

    it("prefills from the synthesis rule, is editable, and sends the edited text", async () => {
      const { client } = setup([KEY, "01"]);
      expect(field().value).toBe("Export verbs print JSON only on stdout; everything else goes to stderr.");
      fireEvent.change(field(), { target: { value: "  My own rule.  " } });
      fireEvent.click(button());
      expect(client.proposeMemory).toHaveBeenCalledWith(KEY, "My own rule.");
      await waitFor(() => expect(field().value).toBe(""));
    });

    it("accepts free text when synthesis offered none, and refuses an empty one", () => {
      const { client } = setup([KEY, "02"]);
      expect(field().value).toBe("");
      expect(button()).toBeDisabled();
      fireEvent.change(field(), { target: { value: "Typed by hand." } });
      fireEvent.click(button());
      expect(client.proposeMemory).toHaveBeenCalledWith(KEY, "Typed by hand.");
    });

    it("follows a rule that arrives later until the developer edits the field", () => {
      const { store } = setup([KEY, "02"]);
      expect(field().value).toBe("");
      const synced = (rule: string) => {
        const data = boardsResponse();
        data.boards.find((b) => b.key === KEY)!.tickets.find((t) => t.nn === "02")!.new_memory_rule = rule;
        act(() => store.load(data));
      };
      synced("First rule.");
      expect(field().value).toBe("First rule.");
      fireEvent.change(field(), { target: { value: "Mine." } });
      synced("Second rule.");
      expect(field().value).toBe("Mine.");
    });

    it("keeps the text when the proposal fails", async () => {
      const { client } = setup([KEY, "02"], { proposeMemory: vi.fn().mockResolvedValue(false) });
      fireEvent.change(field(), { target: { value: "Keep me." } });
      fireEvent.click(button());
      await waitFor(() => expect(button()).toBeEnabled());
      expect(client.proposeMemory).toHaveBeenCalledTimes(1);
      expect(field().value).toBe("Keep me.");
    });
  });
});
