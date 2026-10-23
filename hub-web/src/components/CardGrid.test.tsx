import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { BoardSynced, Devbox, UiEvent, Usage } from "../protocol/types";
import { createStore } from "../store/store";
import { boardsResponse, fixture } from "../test/fixtures";
import { CardGrid } from "./CardGrid";
import { StoreProvider } from "./StoreContext";

function renderGrid(prepare?: (store: ReturnType<typeof createStore>) => void) {
  const store = createStore();
  store.load(boardsResponse());
  prepare?.(store);
  render(
    <StoreProvider store={store}>
      <CardGrid />
    </StoreProvider>,
  );
  return store;
}

const boardSection = (repo: string) => screen.getByRole("region", { name: new RegExp(`^tk-hub ${repo}`) });

describe("CardGrid", () => {
  it("draws a board bar per board: id pill, title, devbox and forge sub-pills", () => {
    renderGrid();
    expect(screen.getAllByRole("region")).toHaveLength(3);
    const bar = boardSection("devboxes");
    expect(within(bar).getByText("tk-hub")).toBeInTheDocument();
    expect(within(bar).getByRole("heading", { name: "devboxes" })).toBeInTheDocument();
    expect(within(bar).getByText("dev-daniel")).toBeInTheDocument();
    expect(within(bar).getByText("none")).toBeInTheDocument();
  });

  it("a card shows the column's glyph in its variant, #NN title, summary, tag pills and checks metric", () => {
    renderGrid();
    const card = screen.getByRole("button", { name: /^01 / });
    const glyph = within(card).getByTitle("HUMAN GATE");
    expect(glyph).toHaveTextContent("◉");
    expect(glyph).toHaveAttribute("data-variant", "human");
    expect(within(card).getByText("#01 tk.sh boards + tk.sh export as JSON")).toBeInTheDocument();
    expect(within(card).getByText(/^Adds the JSON export verbs/)).toHaveTextContent(
      "board.md and retro-results.md carried as text or null.",
    );
    expect(within(card).getAllByTestId("tag").map((t) => t.textContent)).toEqual(["HUMAN GATE", "sonnet/medium"]);
    expect(within(card).getByText("tests pass · 5/5 · rev ✓")).toBeInTheDocument();
  });

  it("each column has its glyph and variant", () => {
    renderGrid();
    const glyphOf = (nn: string) =>
      screen.getByRole("button", { name: new RegExp(`^${nn} `) }).querySelector("[data-variant]")!;
    expect([glyphOf("05"), glyphOf("02"), glyphOf("03"), glyphOf("06"), glyphOf("27"), glyphOf("04")].map(
      (g) => [g.textContent, g.getAttribute("data-variant")],
    )).toEqual([
      ["!", "needs"],
      ["◑", "review"],
      ["⇡", "pr"],
      ["◐", "working"],
      ["○", "none"],
      ["✓", "none"],
    ]);
  });

  it("an unlaunched ticket shows its suggested model, a PR its CI checks", () => {
    renderGrid();
    const unlaunched = screen.getByRole("button", { name: /^27 / });
    expect(within(unlaunched).getAllByTestId("tag").map((t) => t.textContent)).toEqual(["FRONTIER", "~sonnet/low"]);
    expect(within(unlaunched).getByText("ready to launch")).toBeInTheDocument(); // the note stands in for a missing summary
    const pr = screen.getByRole("button", { name: /^03 / });
    expect(within(pr).getByText(/· ci 1\/2$/)).toBeInTheDocument();
  });

  it("greys a stale board and shows when it was last seen", () => {
    renderGrid((store) => store.apply(fixture("board-synced-stale")));
    const section = boardSection("ticket-board");
    expect(section).toHaveAttribute("data-stale", "true");
    expect(within(section).getByText("stale · last seen 2026-10-23 13:10Z")).toBeInTheDocument();
    expect(boardSection("devboxes")).toHaveAttribute("data-stale", "false");
  });

  it("the devbox sub-pill shows the box's health", () => {
    const stale = fixture<BoardSynced>("board-synced-stale");
    renderGrid((store) =>
      store.apply({ ...stale, board: { ...stale.board, key: "dev-acme/ticket-board/tk-hub", devbox: "dev-acme" } }),
    );
    const daniel = within(boardSection("devboxes")).getByText("dev-daniel").closest("[data-health]");
    expect(daniel).toHaveAttribute("data-health", "ok");
    const acme = screen.getByText("dev-acme").closest("[data-health]");
    expect(acme).toHaveAttribute("data-health", "refused");
    expect(acme).toHaveAttribute("title", expect.stringContaining("major differs"));
  });

  afterEach(() => vi.useRealTimers());
  const at = (iso: string) => vi.useFakeTimers({ toFake: ["Date"], now: new Date(iso) });

  it("the devbox sub-pill shows the usage meter, reset times on hover", () => {
    at("2026-10-23T14:05:00Z"); // the fixture's windows are still ahead
    renderGrid();
    const meter = within(boardSection("devboxes")).getByText("5h 42% · wk 72%");
    expect(meter).toHaveAttribute("data-usage", "ok");
    expect(meter).toHaveAttribute("title", expect.stringContaining("5h resets 2026-10-23 17:00Z"));
    expect(meter).toHaveAttribute("title", expect.stringContaining("wk resets 2026-10-27 09:00Z"));
  });

  it("marks usage that is unknown or stale", () => {
    const online = fixture<Devbox & { type: string }>("devbox-status-online");
    const withUsage = (usage: Usage) =>
      renderGrid((store) => store.apply({ ...online, health: { ...online.health!, usage } } as UiEvent));
    const pill = () => within(boardSection("devboxes"));

    cleanup();
    withUsage({ state: "unknown", five_hour: null, seven_day: null, updated_at: null });
    expect(pill().getByText("usage unknown")).toHaveAttribute("data-usage", "unknown");

    cleanup();
    withUsage({ ...online.health!.usage!, state: "stale" });
    const stale = pill().getByText("5h 42% · wk 72% (stale)");
    expect(stale).toHaveAttribute("data-usage", "stale");
    expect(stale).toHaveAttribute("title", expect.stringContaining("updated 2026-10-23 14:01Z"));
  });

  it("says a window already reset in the past tense", () => {
    at("2026-10-23T14:05:00Z");
    const online = fixture<Devbox & { type: string }>("devbox-status-online");
    const usage: Usage = {
      ...online.health!.usage!,
      five_hour: { used_percentage: 0, resets_at: "2020-01-01T00:00:00Z" },
      seven_day: { used_percentage: 72, resets_at: "2099-01-01T00:00:00Z" },
    };
    renderGrid((store) => store.apply({ ...online, health: { ...online.health!, usage } } as UiEvent));

    const meter = within(boardSection("devboxes")).getByText("5h 0% · wk 72%");
    expect(meter).toHaveAttribute("title", expect.stringContaining("5h reset 2020-01-01 00:00Z"));
    expect(meter).toHaveAttribute("title", expect.stringContaining("wk resets 2099-01-01 00:00Z"));
  });

  it("follows the store's filters", () => {
    renderGrid((store) => store.setFilter({ stage: "frontier" }));
    expect(screen.getAllByRole("button").filter((e) => e.tagName === "ARTICLE").map((a) => a.getAttribute("aria-label"))).toEqual(["27 Hub settings page"]);
  });

  it("says so when nothing matches", () => {
    renderGrid((store) => store.setFilter({ devbox: "dev-acme" }));
    expect(screen.getByText("No tickets match these filters.")).toBeInTheDocument();
  });
});
