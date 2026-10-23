import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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
  Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } }));

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
    expect(fetch).toHaveBeenCalledWith("https://tk-hub.example.ts.net/api/boards", expect.anything());
    expect(store.view().counts.all).toBe(14);
    expect(FakeSocket.all.map((s) => s.url)).toEqual(["wss://tk-hub.example.ts.net/ws/ui"]);
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
    expect(fetch).toHaveBeenCalledWith("https://tk-hub.example.ts.net/api/boards", expect.anything());

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
    FakeSocket.all[0].send({ type: "board_removed", board_key: "dev-daniel/ticket-board/tk-hub" }); // the old socket is deaf
    FakeSocket.all[0].drop(); // and never reconnects
    await vi.advanceTimersByTimeAsync(60_000);
    expect(FakeSocket.all).toHaveLength(2);
    expect(store.view().counts.all).toBe(14);
    FakeSocket.all[1].send({ type: "board_removed", board_key: "dev-daniel/devboxes/tk-hub" });
    expect(store.view().counts.all).toBe(11);
  });

  it("scan posts the devbox scope and toasts that it was sent", async () => {
    const { store, fetch, client } = setup();
    await client.scan("dev-daniel");
    await client.scan(null);
    const posts = fetch.mock.calls.filter(([url]) => url.endsWith("/api/scan"));
    expect(posts.map(([, init]) => [init?.method, JSON.parse(String(init?.body))])).toEqual([
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
    fetch.mockImplementationOnce(() => json({ detail: "dev-acme is offline" }, 409));
    await client.scan("dev-acme");
    expect(store.view().toasts.map((t) => [t.kind, t.text])).toEqual([
      ["error", "↻ Scan failed: dev-acme is offline"],
    ]);
  });

  it("scan toasts a network failure", async () => {
    const { store, fetch, client } = setup();
    fetch.mockImplementationOnce(() => Promise.reject(new TypeError("Failed to fetch")));
    await client.scan(null);
    expect(store.view().toasts[0]).toMatchObject({ kind: "error", text: "↻ Scan failed: Failed to fetch" });
  });
});
