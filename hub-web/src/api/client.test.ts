import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import Ajv2020 from "ajv/dist/2020";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { TicketMoved } from "../protocol/types";
import { createStore } from "../store/store";
import { boardsResponse, fixture } from "../test/fixtures";
import { createHubClient, type SocketLike } from "./client";

/** A WebSocket stand-in: tests open, feed and drop it by hand. */
class FakeSocket implements SocketLike {
  static all: FakeSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: unknown }) => void) | null = null;
  onclose: (() => void) | null = null;
  closed = false;
  constructor(readonly url: string) {
    FakeSocket.all.push(this);
  }
  close() {
    this.closed = true;
  }
  open() {
    this.onopen?.();
  }
  send(msg: unknown) {
    this.onmessage?.({ data: JSON.stringify(msg) });
  }
  drop() {
    this.onclose?.();
  }
}

const json = (body: unknown, status = 200) =>
  Promise.resolve(
    new Response(JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    }),
  );

function setup() {
  const store = createStore();
  const fetch = vi.fn((url: string, _init?: RequestInit) =>
    url.endsWith("/api/boards") ? json(boardsResponse()) : json({}, 202),
  );
  const client = createHubClient(store, {
    origin: "https://tk-hub.example.ts.net",
    fetch: fetch as unknown as typeof globalThis.fetch,
    WebSocket: FakeSocket,
  });
  return { store, fetch, client };
}

beforeEach(() => {
  FakeSocket.all = [];
  vi.useFakeTimers();
});
afterEach(() => vi.useRealTimers());

describe("hub client", () => {
  it("loads /api/boards into the store and opens /ws/ui on the same host", async () => {
    const { store, fetch, client } = setup();
    await client.start();
    expect(fetch).toHaveBeenCalledWith(
      "https://tk-hub.example.ts.net/api/boards",
      expect.anything(),
    );
    expect(store.view().counts.all).toBe(14);
    expect(FakeSocket.all.map((s) => s.url)).toEqual([
      "wss://tk-hub.example.ts.net/ws/ui",
    ]);
  });

  it("feeds every socket message to the store", async () => {
    const { store, client } = setup();
    await client.start();
    FakeSocket.all[0].open();
    FakeSocket.all[0].send(fixture("board-removed"));
    FakeSocket.all[0].send(fixture("ticket-moved"));
    expect(store.view().justMoved.size).toBe(1);
  });

  it("ignores a message that is not JSON", async () => {
    const { store, client } = setup();
    await client.start();
    FakeSocket.all[0].onmessage?.({ data: "not json" });
    expect(store.view().counts.all).toBe(14);
  });

  it("reconnects after a drop with growing backoff, and reloads boards once back", async () => {
    const { fetch, client } = setup();
    await client.start();
    FakeSocket.all[0].open();

    FakeSocket.all[0].drop();
    expect(FakeSocket.all).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1000);
    expect(FakeSocket.all).toHaveLength(2);

    FakeSocket.all[1].drop(); // never opened: the next wait doubles
    await vi.advanceTimersByTimeAsync(1999);
    expect(FakeSocket.all).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(FakeSocket.all).toHaveLength(3);

    fetch.mockClear();
    FakeSocket.all[2].open(); // events may have been missed while down
    await vi.advanceTimersByTimeAsync(0);
    expect(fetch).toHaveBeenCalledWith(
      "https://tk-hub.example.ts.net/api/boards",
      expect.anything(),
    );

    FakeSocket.all[2].drop(); // a successful open resets the backoff
    await vi.advanceTimersByTimeAsync(1000);
    expect(FakeSocket.all).toHaveLength(4);
  });

  it("stop closes the socket and does not reconnect", async () => {
    const { client } = setup();
    await client.start();
    client.stop();
    expect(FakeSocket.all[0].closed).toBe(true);
    FakeSocket.all[0].drop();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(FakeSocket.all).toHaveLength(1);
  });

  it("stop then start leaves exactly one live socket", async () => {
    const { store, client } = setup();
    await client.start();
    FakeSocket.all[0].open();
    client.stop();
    await client.start();
    FakeSocket.all[1].open();
    FakeSocket.all[0].send({
      type: "board_removed",
      board_key: "dev-daniel/ticket-board/tk-hub",
    }); // the old socket is deaf
    FakeSocket.all[0].drop(); // and never reconnects
    await vi.advanceTimersByTimeAsync(60_000);
    expect(FakeSocket.all).toHaveLength(2);
    expect(store.view().counts.all).toBe(14);
    FakeSocket.all[1].send({
      type: "board_removed",
      board_key: "dev-daniel/devboxes/tk-hub",
    });
    expect(store.view().counts.all).toBe(11);
  });

  it("scan posts the devbox scope and toasts that it was sent", async () => {
    const { store, fetch, client } = setup();
    await client.scan("dev-daniel");
    await client.scan(null);
    const posts = fetch.mock.calls.filter(([url]) => url.endsWith("/api/scan"));
    expect(
      posts.map(([, init]) => [init?.method, JSON.parse(String(init?.body))]),
    ).toEqual([
      ["POST", { devbox: "dev-daniel" }],
      ["POST", { devbox: null }],
    ]);
    expect(store.view().toasts.map((t) => [t.kind, t.text])).toEqual([
      ["ok", "↻ Rescanning ~/work/*/.scratch on dev-daniel..."],
      ["ok", "↻ Rescanning ~/work/*/.scratch on all devboxes..."],
    ]);
  });

  it("scan toasts the hub's error when the post fails", async () => {
    const { store, fetch, client } = setup();
    fetch.mockImplementationOnce(() =>
      json({ detail: "dev-acme is offline" }, 409),
    );
    await client.scan("dev-acme");
    expect(store.view().toasts.map((t) => [t.kind, t.text])).toEqual([
      ["error", "↻ Scan failed: dev-acme is offline"],
    ]);
  });

  it("scan toasts a network failure", async () => {
    const { store, fetch, client } = setup();
    fetch.mockImplementationOnce(() =>
      Promise.reject(new TypeError("Failed to fetch")),
    );
    await client.scan(null);
    expect(store.view().toasts[0]).toMatchObject({
      kind: "error",
      text: "↻ Scan failed: Failed to fetch",
    });
  });
});

describe("ticket detail endpoints", () => {
  it("GETs report and activity with each board_key segment encoded", async () => {
    const fetch = vi.fn(
      async () => new Response(JSON.stringify([]), { status: 200 }),
    );
    const client = createHubClient(createStore(), {
      origin: "https://hub",
      fetch: fetch as unknown as typeof globalThis.fetch,
    });
    await client.activity("dev one/repo/board", "01");
    await client.report("dev-daniel/repo/board", "01");
    expect(fetch.mock.calls.map((c) => (c as unknown as [string])[0])).toEqual([
      "https://hub/api/boards/dev%20one/repo/board/tickets/01/activity",
      "https://hub/api/boards/dev-daniel/repo/board/tickets/01/report",
    ]);
  });

  it("returns the events of the hub's activity response", async () => {
    const events = [{ from: "backlog", to: "working", at: "2026-10-23T14:02:00Z" }];
    const body = { board_key: "dev-daniel/repo/board", nn: "01", events };
    const fetch = vi.fn(async () => new Response(JSON.stringify(body), { status: 200 }));
    const client = createHubClient(createStore(), { origin: "https://hub", fetch: fetch as unknown as typeof globalThis.fetch });
    expect(await client.activity("dev-daniel/repo/board", "01")).toEqual(events);
  });
});

describe("setArchived", () => {
  it("POSTs {archived} to the encoded per-segment key route and toasts ok", async () => {
    const { store, fetch, client } = setup();
    await client.setArchived("dev box/my repo/slug#1", true);
    const [url, init] = fetch.mock.calls[0];
    expect(url).toBe(
      "https://tk-hub.example.ts.net/api/boards/dev%20box/my%20repo/slug%231/archive",
    );
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init?.body as string)).toEqual({ archived: true });
    expect(store.view().toasts.at(-1)).toMatchObject({ kind: "ok" });
  });

  it("toasts the error on 409", async () => {
    const { store, fetch, client } = setup();
    fetch.mockImplementationOnce(() => json({ detail: "devbox offline" }, 409));
    await client.setArchived("a/b/c", false);
    expect(store.view().toasts.at(-1)).toMatchObject({
      kind: "error",
      text: expect.stringContaining("devbox offline"),
    });
  });
});

describe("syncBox", () => {
  it("POSTs {force} to the devbox's sync route and toasts ok", async () => {
    const { store, fetch, client } = setup();
    await client.syncBox("dev box", true);
    const [url, init] = fetch.mock.calls[0];
    expect(url).toBe("https://tk-hub.example.ts.net/api/devboxes/dev%20box/sync");
    expect(JSON.parse(init?.body as string)).toEqual({ force: true });
    expect(store.view().toasts.at(-1)).toMatchObject({ kind: "ok" });
  });
});

describe("refine and setTaskDone", () => {
  it("refine POSTs {comment} to the ticket's comment route", async () => {
    const { fetch, client } = setup();
    expect(await client.refine("dev box/repo/b", "01", "Mention the cap.")).toBe(true);
    const [url, init] = fetch.mock.calls[0];
    expect(url).toBe("https://tk-hub.example.ts.net/api/boards/dev%20box/repo/b/tickets/01/comment");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init?.body as string)).toEqual({ comment: "Mention the cap." });
  });

  it("setTaskDone POSTs {text, done} to the ticket's tasks route", async () => {
    const { fetch, client } = setup();
    expect(await client.setTaskDone("a/b/c", "07", "Tick a box", true)).toBe(true);
    const [url, init] = fetch.mock.calls[0];
    expect(url).toBe("https://tk-hub.example.ts.net/api/boards/a/b/c/tickets/07/tasks");
    expect(JSON.parse(init?.body as string)).toEqual({ text: "Tick a box", done: true });
  });

  it("both toast the hub's error and return false", async () => {
    const { store, fetch, client } = setup();
    fetch.mockImplementationOnce(() => json({ detail: "devbox dev-x is offline" }, 409));
    expect(await client.refine("a/b/c", "01", "")).toBe(false);
    expect(store.view().toasts.at(-1)).toMatchObject({ kind: "error", text: expect.stringContaining("offline") });
    fetch.mockImplementationOnce(() => json({ detail: "no ticket" }, 404));
    expect(await client.setTaskDone("a/b/c", "01", "x", true)).toBe(false);
    expect(store.view().toasts.at(-1)).toMatchObject({ kind: "error", text: expect.stringContaining("no ticket") });
  });
});

describe("ticket actions", () => {
  const KEY = "dev-daniel/ticket-board/tk-hub";
  const sent = (nn: string, method = "launch_ticket") => ({ id: `r-${nn}`, devbox: "dev-daniel", method, board_key: KEY, nn });

  it("launch posts a manual launch_ticket and tracks its rpc until the result", async () => {
    const { store, fetch, client } = setup();
    fetch.mockImplementationOnce(() => json({ rpcs: [sent("27")] }, 202));
    await client.launch(KEY, "27", { type: "fix", model: "opus", effort: "high" });
    const [url, init] = fetch.mock.calls[0];
    expect(url).toBe(`https://tk-hub.example.ts.net/api/boards/${KEY}/tickets/27/action`);
    expect(JSON.parse(String(init?.body))).toEqual({
      method: "launch_ticket",
      params: { type: "fix", model: "opus", effort: "high", manual: true },
    });
    expect(store.view().pending).toEqual([sent("27")]);
    store.apply({ type: "rpc_result", id: "r-27", ok: true, exit_code: 0, output_tail: "RECORDED 27" });
    expect(store.view().pending).toEqual([]);
  });

  it("a refused action toasts the hub's reason and tracks nothing", async () => {
    const { store, fetch, client } = setup();
    fetch.mockImplementationOnce(() => json({ detail: "devbox dev-daniel is not connected" }, 409));
    expect(await client.say(KEY, "05", "hi")).toBeNull();
    expect(store.view().toasts.map((t) => t.text)).toEqual(["↵ Send failed: devbox dev-daniel is not connected"]);
    expect(store.view().pending).toEqual([]);
  });

  it("frontier posts to the board, tracks each launch and says what it launched and skipped", async () => {
    const { store, fetch, client } = setup();
    fetch.mockImplementationOnce(() =>
      json({ rpcs: [sent("08"), sent("27")], skipped: [{ nn: "09", reason: "no suggested model/effort" }] }, 202),
    );
    await client.frontier(KEY);
    expect(fetch.mock.calls[0][0]).toBe(`https://tk-hub.example.ts.net/api/boards/${KEY}/frontier`);
    expect(store.view().pending.map((p) => p.nn)).toEqual(["08", "27"]);
    expect(store.view().toasts.map((t) => [t.kind, t.text])).toEqual([
      ["warn", "▶ Frontier: launching #08, #27; skipped #09 (no suggested model/effort)"],
    ]);
  });

  it("refreshBrief posts to the board's brief route and tracks the rpc", async () => {
    const { store, fetch, client } = setup();
    fetch.mockImplementationOnce(() => json({ rpcs: [sent("")] }, 202));
    expect(await client.refreshBrief(KEY)).toBe(true);
    expect(fetch.mock.calls[0][0]).toBe(`https://tk-hub.example.ts.net/api/boards/${KEY}/brief`);
    expect(fetch.mock.calls[0][1]?.method).toBe("POST");
    expect(store.view().pending).toHaveLength(1);
  });

  it("a refused brief refresh toasts and returns false", async () => {
    const { store, fetch, client } = setup();
    fetch.mockImplementationOnce(() => json({ detail: "devbox dev-daniel is offline" }, 409));
    expect(await client.refreshBrief(KEY)).toBe(false);
    expect(store.view().toasts.at(-1)).toMatchObject({ kind: "error", text: expect.stringContaining("offline") });
  });

  it("a result that beats its POST's answer leaves nothing pending", async () => {
    const { store, fetch, client } = setup();
    fetch.mockImplementationOnce(() => {
      store.apply({ type: "rpc_result", id: "r-27", ok: true, exit_code: 0, output_tail: "" });
      return json({ rpcs: [sent("27")] }, 202);
    });
    await client.launch(KEY, "27", { type: "feat", model: "sonnet", effort: "low" });
    expect(store.view().pending).toEqual([]);
  });

  describe("REST reads, shaped as protocol/schema.json says the hub answers", () => {
    const schema = JSON.parse(readFileSync(resolve(__dirname, "../../../protocol/schema.json"), "utf8"));
    const ajv = new Ajv2020({ allErrors: true });
    ajv.addKeyword("x-protocol-version");
    ajv.addSchema(schema);
    /** The stubbed answer, checked against `$defs/<def>` first. */
    function answering(def: string, body: object) {
      const valid = ajv.getSchema(`${schema.$id}#/$defs/${def}`)!;
      expect(valid(body), JSON.stringify(valid.errors)).toBe(true);
      const store = createStore();
      const fetch = vi.fn(() => json(body));
      return createHubClient(store, { origin: "https://tk-hub.example.ts.net", fetch: fetch as unknown as typeof globalThis.fetch, WebSocket: FakeSocket });
    }
    const KEY = "dev-daniel/ticket-board/tk-hub";

    it("activity gives the ticket's events from rest_activity", async () => {
      const moved = fixture<TicketMoved>("ticket-moved");
      const client = answering("rest_activity", { board_key: moved.board_key, nn: moved.nn, events: [moved] });
      const events = await client.activity(moved.board_key, moved.nn);
      expect(events).toEqual([moved]);
    });

    it("report gives rest_report as is, a missing body as null", async () => {
      const body = { board_key: KEY, nn: "27", title: "Hub settings page", full_report_md: null, body: null };
      const client = answering("rest_report", body);
      expect(await client.report(KEY, "27")).toEqual(body);
    });
  });
});
