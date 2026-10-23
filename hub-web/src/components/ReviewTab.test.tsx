import { readFileSync } from "node:fs";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { DiffFileResponse, DiffListResponse } from "../protocol/types";
import { ReviewTab } from "./ReviewTab";

type Load = Parameters<typeof ReviewTab>[0]["load"];
const KEY = "dev-daniel/ticket-board/tk-hub";
const HASH = "sha256:" + "a".repeat(64);

const LIST: DiffListResponse = {
  board_key: KEY,
  nn: "25",
  base: "abc123",
  diff_hash: HASH,
  files: [
    { path: "src/app.ts", status: "M", added: 2, removed: 1, binary: false },
    { path: "logo.png", status: "M", added: 0, removed: 0, binary: true },
    { path: "big.txt", status: "A", added: 9000, removed: 0, binary: false },
    { path: "vendor/lib/", status: "A", added: 0, removed: 0, binary: false, nested_repo: true },
    { path: "new.ts", old_path: "old.ts", status: "R", added: 0, removed: 0, binary: false },
  ],
};

const DIFF = [
  "diff --git a/src/app.ts b/src/app.ts",
  "index 111..222 100644",
  "--- a/src/app.ts",
  "+++ b/src/app.ts",
  "@@ -1,2 +1,3 @@",
  " keep",
  "-old line",
  "+new line that is very long and has to wrap instead of scrolling the page sideways " + "x".repeat(200),
].join("\n");

function fileResponse(over: Partial<DiffFileResponse>): DiffFileResponse {
  return { ...LIST, ...LIST.files[0], files: undefined, diff: DIFF, stub: null, ...over } as DiffFileResponse;
}

function setup(diff = vi.fn(), diffs: Record<string, string> = {}) {
  diff.mockImplementation(async (_k: string, _nn: string, file?: string) => {
    if (!file) return LIST;
    if (file === "logo.png") return fileResponse({ path: "logo.png", binary: true, diff: "", stub: "binary" });
    if (file === "big.txt") return fileResponse({ path: "big.txt", status: "A", added: 9000, diff: "", stub: "over_cap" });
    if (file === "vendor/lib/")
      return fileResponse({ path: "vendor/lib/", status: "A", diff: "", stub: "nested_repo", nested_repo: true });
    return fileResponse({ diff: diffs[file] ?? DIFF });
  });
  render(<ReviewTab boardKey={KEY} nn="25" load={diff as unknown as Load} />);
  return diff;
}

describe("ReviewTab", () => {
  it("lists the files with status and stats, and loads no diff until one is expanded", async () => {
    const load = setup();
    const app = await screen.findByRole("button", { name: /src\/app\.ts/ });
    expect(app).toHaveTextContent("M");
    expect(app).toHaveTextContent("+2");
    expect(app).toHaveTextContent("-1");
    expect(screen.getByRole("button", { name: /old\.ts/ })).toHaveTextContent("R");
    expect(app).toHaveAttribute("aria-expanded", "false");
    expect(load).toHaveBeenCalledTimes(1);
    expect(load).toHaveBeenCalledWith(KEY, "25");
  });

  it("loads a file's diff when expanded, once, and colours added and removed lines", async () => {
    const load = setup();
    const app = await screen.findByRole("button", { name: /src\/app\.ts/ });
    fireEvent.click(app);
    expect(app).toHaveAttribute("aria-expanded", "true");
    const added = await screen.findByText(/^new line/);
    expect(load).toHaveBeenLastCalledWith(KEY, "25", "src/app.ts");
    expect(added.closest("td")).toHaveClass("diff-code-insert");
    expect(screen.getByText("old line").closest("td")).toHaveClass("diff-code-delete");
    expect(screen.getByText("keep").closest("td")).toHaveClass("diff-code-normal");

    fireEvent.click(app); // collapse
    expect(screen.queryByText("old line")).not.toBeInTheDocument();
    fireEvent.click(app); // expand again: no second request
    expect(await screen.findByText("old line")).toBeInTheDocument();
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("renders a diff that has no `diff --git` header", async () => {
    setup(vi.fn(), { "src/app.ts": "@@ -1,2 +1,2 @@\n keep\n-old line\n+new line\n" });
    fireEvent.click(await screen.findByRole("button", { name: /src\/app\.ts/ }));
    expect((await screen.findByText("new line")).closest("td")).toHaveClass("diff-code-insert");
  });

  it("says binary, not 'no textual changes', for a diff git printed as `Binary files differ`", async () => {
    const text = "diff --git a/src/app.ts b/src/app.ts\nindex 111..222 100644\nBinary files a/src/app.ts and b/src/app.ts differ\n";
    setup(vi.fn(), { "src/app.ts": text });
    fireEvent.click(await screen.findByRole("button", { name: /src\/app\.ts/ }));
    expect(await screen.findByText(/Binary file/)).toBeInTheDocument();
    expect(screen.queryByText(/No textual changes/)).not.toBeInTheDocument();
  });

  it("shows a stub with the stats for a binary or over-cap file, and for a nested repo", async () => {
    setup();
    fireEvent.click(await screen.findByRole("button", { name: /logo\.png/ }));
    expect(await screen.findByText(/binary file/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /big\.txt/ }));
    const stub = await screen.findByText(/too large/i);
    expect(stub).toHaveTextContent("+9000");
    const nested = screen.getByRole("button", { name: /vendor\/lib\// });
    expect(nested).toHaveTextContent(/repo/i);
    fireEvent.click(nested);
    expect(await screen.findByText(/nested git repository/i)).toBeInTheDocument();
  });

  it("wraps long lines instead of scrolling sideways (narrow viewport)", async () => {
    setup();
    fireEvent.click(await screen.findByRole("button", { name: /src\/app\.ts/ }));
    const line = await screen.findByText(/^new line/);
    const region = line.closest("[data-diff]") as HTMLElement;
    expect(region).not.toBeNull();
    // jsdom has no layout: the contract is the CSS module's wrapping rules
    // over the library's (`.diff-code` is pre-wrap + break-all by default).
    const css = readFileSync(`${import.meta.dirname}/ReviewTab.module.css`, "utf8");
    expect(css).toMatch(/:global\(\.diff-code\)\s*{[^}]*white-space:\s*pre-wrap/);
    expect(css).toMatch(/:global\(\.diff-code\)\s*{[^}]*overflow-wrap:\s*anywhere/);
    expect(css).toMatch(/:global\(\.diff-code\)\s*{[^}]*word-break:\s*normal/);
    expect(css).not.toMatch(/min-width:\s*\d+px/);
    expect(css).not.toMatch(/overflow-x:\s*(auto|scroll)/);
    expect(css).not.toMatch(/#[0-9a-fA-F]{3,6}\b/); // colours come from tokens
  });

  it("derives the diff colours from the icon palette, with no colour literals", () => {
    const tokens = readFileSync(`${import.meta.dirname}/../styles/tokens.css`, "utf8");
    const diff = tokens.slice(tokens.indexOf("--diff-add-fg"), tokens.indexOf("color-scheme"));
    expect(diff).toMatch(/--diff-add-bg:\s*color-mix\([^)]*var\(--icon-working\)/);
    expect(diff).toMatch(/--diff-remove-edit-bg:\s*color-mix\([^)]*var\(--icon-urgent\)/);
    expect(diff).not.toMatch(/#[0-9a-fA-F]{3,6}\b|rgb\(/);
  });

  it("links each row to its panel with ids that survive spaces in the path", async () => {
    const list = { ...LIST, files: [{ path: "my docs/a b.md", status: "M", added: 1, removed: 0, binary: false }] };
    const load = vi.fn(async (_k: string, _nn: string, file?: string) =>
      file ? fileResponse({ path: "my docs/a b.md" }) : list,
    );
    render(<ReviewTab boardKey={KEY} nn="25" load={load as unknown as Load} />);
    const row = await screen.findByRole("button", { name: /a b\.md/ });
    fireEvent.click(row);
    const panel = await screen.findByRole("region", { name: "my docs/a b.md" });
    const id = row.getAttribute("aria-controls")!;
    expect(id).not.toMatch(/\s/);
    expect(panel.id).toBe(id);
  });

  it("says so when there are no changes, and when the diff cannot be loaded", async () => {
    const empty = vi.fn().mockResolvedValue({ ...LIST, files: [] });
    const { unmount } = render(<ReviewTab boardKey={KEY} nn="25" load={empty as unknown as Load} />);
    expect(await screen.findByText(/no uncommitted changes/i)).toBeInTheDocument();
    unmount();
    const failing = vi.fn().mockRejectedValue(new Error("devbox dev-daniel is offline"));
    render(<ReviewTab boardKey={KEY} nn="25" load={failing as unknown as Load} />);
    expect(await screen.findByText(/offline/)).toBeInTheDocument();
  });

  it("notes when the daemon cut the file list short", async () => {
    const cut = vi.fn().mockResolvedValue({ ...LIST, truncated: true, total_files: 1500 });
    render(<ReviewTab boardKey={KEY} nn="25" load={cut as unknown as Load} />);
    expect(await screen.findByText(/first \d+ of 1500 changed files/)).toBeInTheDocument();
  });

  it("shows no truncation note for a complete list", async () => {
    const full = vi.fn().mockResolvedValue({ ...LIST, truncated: false, total_files: LIST.files.length });
    render(<ReviewTab boardKey={KEY} nn="25" load={full as unknown as Load} />);
    await screen.findByRole("button", { name: /src\/app\.ts/ });
    expect(screen.queryByText(/changed files; the rest/)).toBeNull();
  });

  it("reports a failed file load in its panel", async () => {
    const load = vi.fn(async (_k: string, _nn: string, file?: string) => {
      if (!file) return LIST;
      throw new Error("src/app.ts is not in ticket 25's diff");
    });
    render(<ReviewTab boardKey={KEY} nn="25" load={load as unknown as Load} />);
    fireEvent.click(await screen.findByRole("button", { name: /src\/app\.ts/ }));
    const panel = await screen.findByRole("region", { name: /src\/app\.ts/ });
    await waitFor(() => expect(within(panel).getByText(/not in ticket 25/)).toBeInTheDocument());
  });
});
