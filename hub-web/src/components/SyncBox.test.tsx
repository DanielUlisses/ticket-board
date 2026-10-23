import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Devbox, SyncResult } from "../protocol/types";
import { boardsResponse } from "../test/fixtures";
import { SyncBox } from "./SyncBox";

function boxWith(patch: { running?: number; last_sync?: SyncResult | null }): Devbox {
  const box = boardsResponse().devboxes[0]!;
  return { ...box, health: { ...box.health!, ...patch } };
}

function setup(patch: Parameters<typeof boxWith>[0] = { running: 0 }) {
  const onSync = vi.fn().mockResolvedValue(undefined);
  const box = boxWith(patch);
  render(<SyncBox name={box.devbox} box={box} onSync={onSync} />);
  return { onSync, box };
}

const open = () => fireEvent.click(screen.getByRole("button", { name: "⟳ Sync box" }));

afterEach(cleanup);

describe("SyncBox", () => {
  it("syncs nothing until the dialog is confirmed", () => {
    const { onSync, box } = setup();
    open();
    expect(onSync).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Sync" }));
    expect(onSync).toHaveBeenCalledWith(box.devbox, false);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("cancel closes the dialog without syncing", () => {
    const { onSync } = setup();
    open();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onSync).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("with tickets running the confirm is Sync anyway and forces", () => {
    const { onSync, box } = setup({ running: 2 });
    open();
    expect(screen.getByRole("dialog")).toHaveTextContent("2 tickets are working or in review");
    fireEvent.click(screen.getByRole("button", { name: "Sync anyway" }));
    expect(onSync).toHaveBeenCalledWith(box.devbox, true);
  });

  it("shows the last sync's exit code, log tail and the daemon version", () => {
    setup({
      running: 0,
      last_sync: {
        started_at: "2026-10-23T14:00:00Z",
        finished_at: "2026-10-23T14:01:00Z",
        exit_code: 3,
        log_path: "/h/sync.log",
        log_tail: "boom: could not install\n",
      },
    });
    expect(screen.getByText("sync failed (exit 3)")).toBeInTheDocument();
    open();
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("boom: could not install");
    expect(dialog).toHaveTextContent("daemon 0.1.0");
  });

  const interrupted: SyncResult = {
    started_at: "2026-10-23T14:00:00Z",
    finished_at: null,
    exit_code: null,
    interrupted: true,
    log_path: "/h/sync.log",
    log_tail: "installing…\n",
  };

  it("an interrupted sync is an error on the badge and in the dialog, not syncing forever", () => {
    setup({ running: 0, last_sync: interrupted });
    const badge = screen.getByText("sync interrupted");
    expect(badge).toHaveAttribute("data-sync", "failed");
    expect(screen.queryByText("syncing…")).toBeNull();
    open();
    expect(screen.getByRole("dialog")).toHaveTextContent("sync interrupted");
  });

  it("a failed sync shows its exit code and the log tail in a visible block on the dialog and the box", () => {
    setup({
      running: 0,
      last_sync: { ...interrupted, exit_code: 3, interrupted: false, log_tail: "boom: could not install\n" },
    });
    const badge = screen.getByText("sync failed (exit 3)");
    expect(badge).toHaveAttribute("data-sync", "failed");
    // The box's expandable log panel, not just a tooltip.
    const panel = screen.getByText("Sync log").closest("details")!;
    expect(panel).toHaveTextContent("boom: could not install");
    expect(panel.querySelector("pre")).not.toBeNull();
    open();
    expect(screen.getByRole("dialog").querySelector("pre")).toHaveTextContent("boom: could not install");
  });

  it("while a sync runs the dialog cannot start another", () => {
    setup({ running: 0, last_sync: { ...interrupted, interrupted: false } });
    expect(screen.getByText("syncing…")).toHaveAttribute("data-sync", "running");
    open();
    expect(screen.getByRole("button", { name: "Sync" })).toBeDisabled();
  });
});
