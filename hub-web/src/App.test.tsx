import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { createHubClient, type SocketLike } from "./api/client";
import { App } from "./App";
import type { TicketMoved } from "./protocol/types";
import { TOAST_MS } from "./components/Toasts";
import { JUST_MOVED_MS, createStore } from "./store/store";
import { boardsResponse, fixture } from "./test/fixtures";

class FakeSocket implements SocketLike {
  static last: FakeSocket | null = null;
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: unknown }) => void) | null = null;
  onclose: (() => void) | null = null;
  constructor(_url: string) {
    FakeSocket.last = this;
  }
  close() {}
  send(msg: unknown) {
    act(() => this.onmessage?.({ data: JSON.stringify(msg) }));
  }
}

async function renderApp() {
  const store = createStore();
  const fetch = vi.fn((url: string, _init?: RequestInit) =>
    Promise.resolve(
      url.endsWith("/api/boards")
        ? new Response(JSON.stringify(boardsResponse()))
        : new Response("{}", { status: 202 }),
    ),
  );
  const client = createHubClient(store, {
    origin: "http://hub.test",
    fetch: fetch as unknown as typeof globalThis.fetch,
    WebSocket: FakeSocket,
  });
  render(<App store={store} client={client} />);
  await waitFor(() => expect(articles().length).toBeGreaterThan(0));
  return { store, fetch };
}

const articles = () => screen.queryAllByRole("button").filter((e) => e.tagName === "ARTICLE");
const pill = (name: RegExp) => screen.getByRole("button", { name });
const cardNumbers = () => articles().map((a) => a.getAttribute("aria-label")!.split(" ")[0]);

describe("App", () => {
  it("shows the navbar: brand, page switch, Scan and a Night placeholder", async () => {
    await renderApp();
    const nav = screen.getByRole("navigation");
    expect(nav).toHaveTextContent("-{|}- tk-hub");
    expect(within(nav).getByRole("button", { name: "Cards" })).toHaveAttribute("aria-pressed", "true");
    expect(within(nav).getByRole("button", { name: /Scan/ })).toBeEnabled();
    expect(within(nav).getByRole("button", { name: /Night/ })).toBeDisabled();
  });

  it("switches between Cards and Live Board", async () => {
    await renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Live Board" }));
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(articles()).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Cards" }));
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("pills show their counts and filter both pages", async () => {
    await renderApp();
    expect([/^All/, /^Needs You/, /^Active/, /^Frontier/].map((n) => pill(n).textContent)).toEqual([
      "All",
      "Needs You (3)",
      "Active (2)",
      "Frontier (1)",
    ]);
    fireEvent.click(pill(/^Needs You/));
    expect(pill(/^Needs You/)).toHaveAttribute("aria-pressed", "true");
    expect(cardNumbers()).toEqual(["01", "03", "05"]);
    fireEvent.click(screen.getByRole("button", { name: "Live Board" }));
    expect(screen.getAllByTitle(/^\d+ /).map((p) => p.title.split(" ")[0])).toEqual(["01", "03", "05"]);
  });

  it("the pills and the Stage multi-select are one filter", async () => {
    await renderApp();
    expect(screen.getByRole("button", { name: "Stage" })).toHaveTextContent("Stages (4)");
    fireEvent.click(screen.getByRole("button", { name: "Stage" }));
    const box = (name: string) => screen.getByRole("checkbox", { name });
    expect(screen.getAllByRole("checkbox").map((c) => c.parentElement!.textContent)).toEqual([
      "Needs You / Human Gate",
      "Active (Working / Review)",
      "Frontier Ready",
      "Blocked",
      "Done",
    ]);
    fireEvent.click(pill(/^Frontier/));
    expect(box("Frontier Ready")).toBeChecked();
    expect(box("Active (Working / Review)")).not.toBeChecked();
    fireEvent.click(box("Active (Working / Review)"));
    expect(pill(/^Active/)).toHaveAttribute("aria-pressed", "false"); // frontier + active: no preset
    fireEvent.click(box("Frontier Ready"));
    expect(pill(/^Active/)).toHaveAttribute("aria-pressed", "true");
    expect(cardNumbers()).toEqual(["02", "06"]);
    fireEvent.click(box("Active (Working / Review)"));
    fireEvent.click(box("Done"));
    expect(screen.getAllByRole("button", { pressed: true }).map((b) => b.textContent)).toEqual(["Cards"]);
    expect(cardNumbers()).toEqual(["04"]);
  });

  it("the Devbox select and Board ticks narrow the counts, not the stage, on both pages", async () => {
    await renderApp();
    const devbox = screen.getByRole("combobox", { name: "Devbox" });
    expect(within(devbox).getAllByRole("option").map((o) => o.textContent)).toEqual([
      "All Devboxes (2)",
      "dev-daniel",
      "dev-acme",
    ]);
    expect(screen.getByRole("button", { name: "Board" })).toHaveTextContent("Boards (3)");
    fireEvent.click(screen.getByRole("button", { name: "Board" }));
    expect(screen.getAllByRole("checkbox").map((c) => c.parentElement!.textContent)).toEqual([
      "tk-hub · ticket-skill",
      "tk-hub · devboxes",
      "tk-hub · ticket-board",
    ]);
    fireEvent.click(screen.getByRole("checkbox", { name: "tk-hub · devboxes" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "tk-hub · ticket-board" }));
    expect(screen.getByRole("button", { name: "Board" })).toHaveTextContent("Boards (1)");
    expect(pill(/^Needs You/)).toHaveTextContent("Needs You (2)");
    expect(cardNumbers()).toEqual(["01", "02", "03"]);
    fireEvent.click(pill(/^Active/));
    expect(pill(/^Needs You/)).toHaveTextContent("Needs You (2)");
    expect(cardNumbers()).toEqual(["02"]);
    fireEvent.click(screen.getByRole("button", { name: "Live Board" }));
    expect(screen.getAllByTitle(/^\d+ /).map((p) => p.title.split(" ")[0])).toEqual(["02"]);
  });

  it("the tab title carries the Needs You count under the Devbox / Board filters and follows WS events", async () => {
    await renderApp();
    expect(document.title).toBe("(3) tk-hub");
    fireEvent.change(screen.getByRole("combobox", { name: "Devbox" }), { target: { value: "dev-acme" } });
    expect(document.title).toBe("tk-hub");
    fireEvent.change(screen.getByRole("combobox", { name: "Devbox" }), { target: { value: "" } });
    FakeSocket.last!.send({ type: "board_removed", board_key: "dev-daniel/devboxes/tk-hub" });
    expect(document.title).toBe("(2) tk-hub");
    FakeSocket.last!.send({ type: "board_removed", board_key: "dev-daniel/ticket-skill/tk-hub" });
    expect(document.title).toBe("tk-hub");
  });

  it("↻ Scan posts for the selected devbox and toasts", async () => {
    const { fetch } = await renderApp();
    fireEvent.change(screen.getByRole("combobox", { name: "Devbox" }), { target: { value: "dev-daniel" } });
    fireEvent.click(screen.getByRole("button", { name: /Scan/ }));
    expect(await screen.findByRole("status")).toHaveTextContent("↻ Rescanning ~/work/*/.scratch on dev-daniel...");
    const post = fetch.mock.calls.find(([url]) => url.endsWith("/api/scan"));
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({ devbox: "dev-daniel" });
  });

  it("an rpc_result arriving on the socket shows as a toast", async () => {
    await renderApp();
    FakeSocket.last!.send(fixture("rpc-result-refused-ui"));
    expect(screen.getByRole("status")).toHaveTextContent("say_agent: failed: no agent: the ticket's agent isn't running");
  });

  it("a toast goes away on its own", async () => {
    const { store } = await renderApp();
    vi.useFakeTimers();
    try {
      act(() => store.toast({ kind: "ok", text: "hello" }));
      expect(screen.getByRole("status")).toHaveTextContent("hello");
      act(() => vi.advanceTimersByTime(TOAST_MS));
      expect(screen.queryByRole("status")).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("a move seen while on Cards has expired by the time Live Board opens", async () => {
    await renderApp();
    vi.useFakeTimers();
    try {
      FakeSocket.last!.send({ ...fixture<TicketMoved>("ticket-moved"), board_key: "dev-daniel/ticket-board/tk-hub", nn: "27", from: "backlog", to: "working" });
      act(() => vi.advanceTimersByTime(JUST_MOVED_MS));
      fireEvent.click(screen.getByRole("button", { name: "Live Board" }));
      expect(screen.getByTitle(/^27 /)).toHaveAttribute("data-moved", "false");
    } finally {
      vi.useRealTimers();
    }
  });
});
