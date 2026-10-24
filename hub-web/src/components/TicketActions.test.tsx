import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { RpcResult } from "../protocol/types";
import { createStore } from "../store/store";
import { boardsResponse } from "../test/fixtures";
import { CardGrid } from "./CardGrid";
import { StoreProvider } from "./StoreContext";
import { PANE_MS, TicketSheet, type DetailApi } from "./TicketSheet";
import { Toasts } from "./Toasts";

const BOARD = "dev-daniel/ticket-board/tk-hub"; // 27 in backlog (sonnet/low), 06 working
const DEVBOXES = "dev-daniel/devboxes/tk-hub"; // 05 at needs

function setup(open: [string, string]) {
  const client: DetailApi = {
    activity: vi.fn().mockResolvedValue([]),
    diff: vi.fn().mockResolvedValue({ files: [] }),
    report: vi.fn().mockResolvedValue({ full_report_md: null, body: "" }),
    launch: vi.fn().mockResolvedValue(undefined),
    proposeMemory: vi.fn().mockResolvedValue(true),
    say: vi.fn().mockResolvedValue("r-say"),
    paneTail: vi.fn().mockResolvedValue(undefined),
    ship: vi.fn().mockResolvedValue(true),
    resolveNoCode: vi.fn().mockResolvedValue(true),
    finishConflict: vi.fn().mockResolvedValue(true),
    abortConflict: vi.fn().mockResolvedValue(true),
    refine: vi.fn().mockResolvedValue(true),
    setTaskDone: vi.fn().mockResolvedValue(true),
  };
  const store = createStore();
  store.load(boardsResponse());
  store.openTicket(...open);
  render(
    <StoreProvider store={store}>
      <TicketSheet client={client} />
      <Toasts />
    </StoreProvider>,
  );
  return { store, client };
}

const footer = () => screen.getByRole("contentinfo", { name: "Actions" });
const launchButton = () => within(footer()).getByRole("button", { name: /Launch/ });
const lock = () => within(footer()).getByRole("button", { name: /model, effort and type/ });

function result(over: Partial<RpcResult>): RpcResult {
  return { type: "rpc_result", id: "r-1", ok: true, exit_code: 0, output_tail: "", devbox: "dev-daniel", ...over };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("launch control", () => {
  it("launches with the ticket's suggested type, model and effort while locked", () => {
    const { client } = setup([BOARD, "27"]);
    expect(within(footer()).getByText("feat · sonnet · low")).toBeInTheDocument();
    expect(within(footer()).queryByRole("combobox")).not.toBeInTheDocument();
    fireEvent.click(launchButton());
    expect(client.launch).toHaveBeenCalledWith(BOARD, "27", { type: "feat", model: "sonnet", effort: "low" });
  });

  it("the lock unlocks type, model and effort selects to override before launching", () => {
    const { client } = setup([BOARD, "27"]);
    fireEvent.click(lock());
    expect(lock()).toHaveAttribute("aria-pressed", "true");
    const type = within(footer()).getByRole("combobox", { name: "Type" });
    const model = within(footer()).getByRole("combobox", { name: "Model" });
    const effort = within(footer()).getByRole("combobox", { name: "Effort" });
    expect([type, model, effort].map((s) => (s as HTMLSelectElement).value)).toEqual(["feat", "sonnet", "low"]);
    fireEvent.change(type, { target: { value: "fix" } });
    fireEvent.change(model, { target: { value: "opus" } });
    fireEvent.change(effort, { target: { value: "high" } });
    fireEvent.click(launchButton());
    expect(client.launch).toHaveBeenLastCalledWith(BOARD, "27", { type: "fix", model: "opus", effort: "high" });
  });

  it("locking again goes back to the suggestions", () => {
    const { client } = setup([BOARD, "27"]);
    fireEvent.click(lock());
    fireEvent.change(within(footer()).getByRole("combobox", { name: "Model" }), { target: { value: "opus" } });
    fireEvent.click(lock());
    expect(within(footer()).queryByRole("combobox")).not.toBeInTheDocument();
    fireEvent.click(launchButton());
    expect(client.launch).toHaveBeenLastCalledWith(BOARD, "27", { type: "feat", model: "sonnet", effort: "low" });
  });

  it("with no suggestion, the unlocked model and effort selects show an empty choice and launch waits for one", () => {
    const { store, client } = setup([BOARD, "27"]);
    const data = boardsResponse();
    const t27 = data.boards.find((b) => b.key === BOARD)!.tickets.find((t) => t.nn === "27")!;
    Object.assign(t27, { smodel: "", seffort: "" });
    act(() => store.load(data));
    fireEvent.click(lock());
    const model = within(footer()).getByRole("combobox", { name: "Model" }) as HTMLSelectElement;
    const effort = within(footer()).getByRole("combobox", { name: "Effort" }) as HTMLSelectElement;
    expect([model.value, effort.value]).toEqual(["", ""]);
    expect(model.selectedOptions[0]).toHaveTextContent("choose…");
    expect(launchButton()).toBeDisabled();
    fireEvent.change(model, { target: { value: "opus" } });
    fireEvent.change(effort, { target: { value: "high" } });
    fireEvent.click(launchButton());
    expect(client.launch).toHaveBeenCalledWith(BOARD, "27", { type: "feat", model: "opus", effort: "high" });
  });

  it("shows a spinner while its launch is pending, then the rpc_result's toast", () => {
    const { store } = setup([BOARD, "27"]);
    act(() => store.track([{ id: "r-1", devbox: "dev-daniel", method: "launch_ticket", board_key: BOARD, nn: "27" }]));
    expect(launchButton()).toBeDisabled();
    expect(launchButton()).toHaveAttribute("aria-busy", "true");
    act(() => store.apply(result({ method: "launch_ticket", board_key: BOARD, nn: "27", output_tail: "RECORDED 27" })));
    expect(launchButton()).toBeEnabled();
    expect(screen.getByRole("status")).toHaveTextContent("launch_ticket: RECORDED 27");
  });

  it("warns when a manual launch went past the box's max_parallel", () => {
    const { store } = setup([BOARD, "27"]);
    act(() =>
      store.apply(
        result({ method: "launch_ticket", board_key: BOARD, nn: "27", output_tail: "RECORDED 27",
                 data: { over_cap: true, running: 5, max_parallel: 5 } }), // prettier-ignore
      ),
    );
    const toast = screen.getByRole("status");
    expect(toast).toHaveAttribute("data-kind", "warn");
    expect(toast).toHaveTextContent("over max_parallel: 5 running, cap 5");
  });
});

describe("relay", () => {
  const relay = () => within(footer()).getByRole("textbox", { name: "Relay (tk.sh say)" });
  const send = () => within(footer()).getByRole("button", { name: /Send/ });

  it("sends the message through tk.sh say and clears it once sent", async () => {
    const { store, client } = setup([DEVBOXES, "05"]);
    expect(send()).toBeDisabled();
    fireEvent.change(relay(), { target: { value: "Allow it, then continue." } });
    await act(async () => fireEvent.click(send()));
    expect(client.say).toHaveBeenCalledWith(DEVBOXES, "05", "Allow it, then continue.");
    act(() => store.track([{ id: "r-say", devbox: "dev-daniel", method: "say_agent", board_key: DEVBOXES, nn: "05" }]));
    expect(send()).toHaveAttribute("aria-busy", "true");
    act(() => store.apply(result({ id: "r-say", method: "say_agent", board_key: DEVBOXES, nn: "05", output_tail: "SENT" })));
    expect(relay()).toHaveValue("");
  });

  it("keeps the message and shows the reason when the agent is at a dialog", async () => {
    const { store } = setup([DEVBOXES, "05"]);
    fireEvent.change(relay(), { target: { value: "Allow it." } });
    await act(async () => fireEvent.click(send()));
    act(() => store.track([{ id: "r-say", devbox: "dev-daniel", method: "say_agent", board_key: DEVBOXES, nn: "05" }]));
    act(() =>
      store.apply(
        result({ id: "r-say", method: "say_agent", board_key: DEVBOXES, nn: "05", ok: false, exit_code: 20,
                 reason: "dialog: the agent is at a dialog or not at its prompt; answer it in its herdr tab" }), // prettier-ignore
      ),
    );
    expect(relay()).toHaveValue("Allow it.");
    expect(screen.getByRole("status")).toHaveTextContent("say_agent: failed: dialog: the agent is at a dialog");
  });

  it("is the primary action on a ticket that needs you; a backlog ticket has none", () => {
    setup([DEVBOXES, "05"]);
    expect(send()).toHaveAttribute("data-primary", "true");
    cleanup();
    setup([BOARD, "27"]);
    expect(within(footer()).queryByRole("textbox")).not.toBeInTheDocument();
  });
});

describe("pane tail", () => {
  it("polls tk.sh show while the sheet is open on an active ticket and shows it live", () => {
    vi.useFakeTimers();
    const { store, client } = setup([BOARD, "06"]);
    expect(client.paneTail).toHaveBeenCalledWith(BOARD, "06", 180);
    const pane = screen.getByRole("region", { name: "Agent pane" });
    act(() =>
      store.apply(result({ method: "pane_tail", board_key: BOARD, nn: "06", data: { pane: "scouting...\nimplementing" } })),
    );
    expect(within(pane).getByText(/implementing/)).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument(); // no toast for a poll
    act(() => vi.advanceTimersByTime(PANE_MS));
    expect(client.paneTail).toHaveBeenCalledTimes(2);
  });

  it("asks again when an answer never comes back", () => {
    vi.useFakeTimers();
    const { client } = setup([BOARD, "06"]);
    expect(client.paneTail).toHaveBeenCalledTimes(1);
    act(() => vi.advanceTimersByTime(PANE_MS * 3 - 1));
    expect(client.paneTail).toHaveBeenCalledTimes(1);
    act(() => vi.advanceTimersByTime(1));
    expect(client.paneTail).toHaveBeenCalledTimes(2);
  });

  it("says why when the ticket has no live agent", () => {
    const { store } = setup([BOARD, "06"]);
    act(() =>
      store.apply(
        result({ method: "pane_tail", board_key: BOARD, nn: "06", ok: false, exit_code: 21,
                 reason: "no agent: the ticket's agent isn't running" }), // prettier-ignore
      ),
    );
    const pane = screen.getByRole("region", { name: "Agent pane" });
    expect(within(pane).getByText("no agent: the ticket's agent isn't running")).toBeInTheDocument();
  });

  it("is not polled for a backlog ticket", () => {
    const { client } = setup([BOARD, "27"]);
    expect(client.paneTail).not.toHaveBeenCalled();
  });
});

describe("frontier", () => {
  it("▶ Frontier in a board bar launches that board's frontier, with a spinner while pending", () => {
    const onFrontier = vi.fn();
    const store = createStore();
    store.load(boardsResponse());
    render(
      <StoreProvider store={store}>
        <CardGrid onFrontier={onFrontier} />
      </StoreProvider>,
    );
    const section = screen.getByRole("region", { name: /^tk-hub ticket-board/ });
    const button = within(section).getByRole("button", { name: /Frontier/ });
    expect(button).toHaveTextContent("▶ Frontier (1)");
    fireEvent.click(button);
    expect(onFrontier).toHaveBeenCalledWith(BOARD);
    act(() => store.track([{ id: "r-9", devbox: "dev-daniel", method: "launch_ticket", board_key: BOARD, nn: "27" }]));
    expect(button).toHaveAttribute("aria-busy", "true");
    // A board with nothing on its frontier has nothing to launch.
    const other = screen.getByRole("region", { name: /^tk-hub devboxes/ });
    expect(within(other).getByRole("button", { name: /Frontier/ })).toBeDisabled();
  });

  it("▶ Frontier shows a spinner and stays disabled while its POST is in flight", async () => {
    let answer!: () => void;
    const onFrontier = vi.fn(() => new Promise<void>((resolve) => (answer = resolve)));
    const store = createStore();
    store.load(boardsResponse());
    render(
      <StoreProvider store={store}>
        <CardGrid onFrontier={onFrontier} />
      </StoreProvider>,
    );
    const section = screen.getByRole("region", { name: /^tk-hub ticket-board/ });
    const button = within(section).getByRole("button", { name: /Frontier/ });
    fireEvent.click(button);
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-busy", "true");
    fireEvent.click(button);
    expect(onFrontier).toHaveBeenCalledTimes(1);
    await act(async () => answer());
    expect(button).not.toBeDisabled();
    expect(button).toHaveAttribute("aria-busy", "false");
  });
});
