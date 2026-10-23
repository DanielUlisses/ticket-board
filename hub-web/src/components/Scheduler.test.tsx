import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { HubSettings, SchedulerEntry } from "../protocol/types";
import { createStore } from "../store/store";
import { fixture } from "../test/fixtures";
import { SchedulerPanel, WavesButton } from "./Scheduler";
import { StoreProvider } from "./StoreContext";

const OFF: HubSettings = { total_cap: 8, waves: false, waves_start: null };

function entry(id: number, patch: Partial<SchedulerEntry>): SchedulerEntry {
  return { ...fixture<{ entry: SchedulerEntry }>("scheduler-log").entry, id, ...patch };
}

function setup(settings: Partial<HubSettings> = {}, entries: SchedulerEntry[] = []) {
  const store = createStore();
  store.apply({ type: "hub_settings", settings: { ...OFF, ...settings } });
  store.loadSchedulerLog(entries);
  const api = { setSettings: vi.fn((_patch: Partial<HubSettings>) => Promise.resolve(true)) };
  render(
    <StoreProvider store={store}>
      <WavesButton api={api} />
      <SchedulerPanel api={api} />
    </StoreProvider>,
  );
  return { store, api };
}

const waves = () => screen.getByRole("button", { name: /waves/i });

describe("▶ Run waves", () => {
  it("is a scheduler control, not a dark-mode toggle", () => {
    setup();
    expect(waves()).toHaveTextContent("▶ Run waves");
    expect(waves()).toHaveAttribute("aria-pressed", "false");
    expect(waves()).toBeEnabled();
    expect(document.body).not.toHaveTextContent(/Night|☾/);
    expect(waves().className).not.toMatch(/primary/);
  });

  it("asks when to start: now by default", () => {
    const { api } = setup();
    fireEvent.click(waves());
    const dialog = screen.getByRole("dialog", { name: "Run waves" });
    expect(within(dialog).getByRole("radio", { name: "Now" })).toBeChecked();
    fireEvent.click(within(dialog).getByRole("button", { name: "Start" }));
    expect(api.setSettings).toHaveBeenCalledWith({ waves: true, waves_start: null });
  });

  it("or at a chosen start time, sent as UTC", () => {
    const { api } = setup();
    fireEvent.click(waves());
    const dialog = screen.getByRole("dialog", { name: "Run waves" });
    fireEvent.click(within(dialog).getByRole("radio", { name: "At" }));
    const start = within(dialog).getByRole("button", { name: "Start" });
    expect(start).toBeDisabled(); // no time picked yet
    fireEvent.change(within(dialog).getByLabelText("Start at"), { target: { value: "2026-10-23T22:30" } });
    fireEvent.click(start);
    const utc = new Date(2026, 9, 23, 22, 30).toISOString().replace(/\.\d{3}Z$/, "Z");
    expect(api.setSettings).toHaveBeenCalledWith({ waves: true, waves_start: utc });
  });

  it("shows that it runs, and stops on a click", () => {
    const { api } = setup({ waves: true });
    expect(waves()).toHaveTextContent("■ Running waves");
    expect(waves()).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(waves());
    expect(api.setSettings).toHaveBeenCalledWith({ waves: false });
  });

  it("shows the start time, with its date when it isn't today", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    try {
      vi.setSystemTime(new Date(2026, 9, 23, 12, 0));
      const at = (d: Date) => d.toISOString().replace(/\.\d{3}Z$/, "Z");
      const { store } = setup({ waves: true, waves_start: at(new Date(2026, 9, 23, 22, 30)) });
      expect(waves()).toHaveTextContent(/^■ Waves at 22:30$/);
      act(() =>
        store.apply({ type: "hub_settings", settings: { ...OFF, waves: true, waves_start: at(new Date(2026, 9, 24, 6, 0)) } }),
      );
      expect(waves()).toHaveTextContent(/^■ Waves at Sat 24 Oct 06:00$/);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("Scheduler panel", () => {
  const panel = () => {
    const open = screen.queryByRole("dialog", { name: "Scheduler" });
    if (open) return open;
    fireEvent.click(screen.getByRole("button", { name: "Scheduler" }));
    return screen.getByRole("dialog", { name: "Scheduler" });
  };

  it("edits the total cap", () => {
    const { store, api } = setup();
    const cap = within(panel()).getByLabelText("Total cap");
    expect(cap).toHaveValue(8);
    fireEvent.change(cap, { target: { value: "5" } });
    fireEvent.click(within(panel()).getByRole("button", { name: "Save" }));
    expect(api.setSettings).toHaveBeenCalledWith({ total_cap: 5 });

    act(() => store.apply({ type: "hub_settings", settings: { ...OFF, total_cap: 3 } }));
    expect(within(panel()).getByLabelText("Total cap")).toHaveValue(3);
  });

  it("lists the decisions, newest first, with their reasons", () => {
    const { store } = setup({}, [
      entry(2, { action: "sent", nn: "27", reason: "frontier" }),
      entry(1, { action: "skipped", nn: "09", reason: "dev-daniel is at max_parallel (5 running, cap 5)" }),
    ]);
    act(() =>
      store.apply({ type: "scheduler_log", entry: entry(3, { action: "waves_off", method: null, board_key: null, nn: null, reason: "nothing left to launch, ship or merge" }) }),
    );
    const rows = within(panel()).getAllByRole("listitem").map((li) => li.textContent);
    expect(rows).toHaveLength(3);
    expect(rows[0]).toContain("waves off");
    expect(rows[0]).toContain("nothing left to launch, ship or merge");
    expect(rows[1]).toContain("sent launch_ticket #27");
    expect(rows[2]).toContain("skipped launch_ticket #09");
    expect(rows[2]).toContain("dev-daniel is at max_parallel (5 running, cap 5)");
  });
});
