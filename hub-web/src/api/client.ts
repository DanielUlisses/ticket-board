// Typed client for the hub: `GET /api/boards` on start, `/ws/ui` kept open
// with backoff reconnects, `POST /api/scan` for ↻ Scan. Everything it learns
// goes into the one store; it holds no board data itself.
//
// ASSUMPTIONS (the hub routes arrive with ticket 08, unwritten when this was):
//   - GET /api/boards -> BoardsResponse `{boards, devboxes}`;
//   - POST /api/scan with `{"devbox": "<name>" | null}` (null: every box), any
//     2xx meaning the rescan was sent; the outcome arrives later as rpc_result;
//   - errors carry FastAPI's `{"detail": "..."}`.
import type { ActivityEvent, BoardsResponse, ReportResponse, UiEvent } from "../protocol/types";
import type { Store } from "../store/store";

/** The part of the browser WebSocket the client uses. */
export interface SocketLike {
  onopen: (() => void) | null;
  onmessage: ((e: { data: unknown }) => void) | null;
  onclose: (() => void) | null;
  close(): void;
}

export interface HubClientDeps {
  /** `https://host[:port]`; the socket uses the matching ws(s) scheme. */
  origin?: string;
  fetch?: typeof fetch;
  WebSocket?: new (url: string) => SocketLike;
}

const BACKOFF_MS = [1000, 2000, 4000, 8000, 15000];

export function createHubClient(store: Store, deps: HubClientDeps = {}) {
  const origin = deps.origin ?? window.location.origin;
  const doFetch = deps.fetch ?? ((...args: Parameters<typeof fetch>) => fetch(...args));
  const Socket = deps.WebSocket ?? (WebSocket as unknown as new (url: string) => SocketLike);
  const wsUrl = origin.replace(/^http/, "ws") + "/ws/ui";

  let socket: SocketLike | null = null;
  let attempt = 0;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let stopped = false;
  let generation = 0; // bumped by start/stop so a superseded start never connects

  async function loadBoards() {
    const res = await doFetch(`${origin}/api/boards`, { headers: { accept: "application/json" } });
    if (!res.ok) throw new Error(`GET /api/boards: HTTP ${res.status}`);
    store.load((await res.json()) as BoardsResponse);
  }

  function connect() {
    const ws = new Socket(wsUrl);
    socket = ws;
    let opened = false;
    ws.onopen = () => {
      // Events sent while we were away are lost: resync from REST.
      if (attempt > 0) loadBoards().catch(() => {});
      opened = true;
      attempt = 0;
    };
    ws.onmessage = (e) => {
      let event: UiEvent;
      try {
        event = JSON.parse(String(e.data)) as UiEvent;
      } catch {
        return;
      }
      store.apply(event);
    };
    ws.onclose = () => {
      if (stopped || socket !== ws) return;
      if (opened) attempt = 0;
      const wait = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)];
      attempt++;
      timer = setTimeout(connect, wait);
    };
  }

  async function errorText(res: Response): Promise<string> {
    try {
      const body = (await res.json()) as { detail?: unknown };
      if (typeof body.detail === "string") return body.detail;
    } catch {
      // not JSON
    }
    return `HTTP ${res.status}`;
  }

  // board_key is `<devbox>/<repo>/<board>`: encode each segment, keep the slashes.
  const ticketUrl = (boardKey: string, nn: string, leaf: string) =>
    `${origin}/api/boards/${boardKey.split("/").map(encodeURIComponent).join("/")}/tickets/${encodeURIComponent(nn)}/${leaf}`;

  async function getJson<T>(url: string): Promise<T> {
    const res = await doFetch(url, { headers: { accept: "application/json" } });
    if (!res.ok) throw new Error(await errorText(res));
    return (await res.json()) as T;
  }

  return {
    /** The ticket's full report and raw body (ticket sheet's report modal). */
    report: (boardKey: string, nn: string) => getJson<ReportResponse>(ticketUrl(boardKey, nn, "report")),
    /** The ticket's column moves, oldest first. */
    activity: (boardKey: string, nn: string) => getJson<ActivityEvent[]>(ticketUrl(boardKey, nn, "activity")),
    /** Loads the boards, then opens the socket (also when the load failed). */
    async start() {
      stopped = false;
      const mine = ++generation;
      try {
        await loadBoards();
      } catch (err) {
        store.toast({ kind: "error", text: `Could not load boards: ${(err as Error).message}` });
      }
      if (mine === generation) connect();
    },
    stop() {
      stopped = true;
      generation++;
      if (timer) clearTimeout(timer);
      if (socket) {
        socket.onopen = socket.onmessage = socket.onclose = null;
        socket.close();
        socket = null;
      }
    },
    /** ↻ Scan: a forced full rescan of one devbox, or every box when null. */
    async scan(devbox: string | null) {
      const scope = devbox ?? "all devboxes";
      try {
        const res = await doFetch(`${origin}/api/scan`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ devbox }),
        });
        if (res.ok) store.toast({ kind: "ok", text: `↻ Rescanning ~/work/*/.scratch on ${scope}...` });
        else store.toast({ kind: "error", text: `↻ Scan failed: ${await errorText(res)}` });
      } catch (err) {
        store.toast({ kind: "error", text: `↻ Scan failed: ${(err as Error).message}` });
      }
    },
    /** Archive or restore a board: the hub asks its box's daemon (box online only). */
    async setArchived(boardKey: string, archived: boolean) {
      const verb = archived ? "Archive" : "Restore";
      try {
        const res = await doFetch(`${origin}/api/boards/${boardKey.split("/").map(encodeURIComponent).join("/")}/archive`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ archived }),
        });
        if (res.ok) store.toast({ kind: "ok", text: `${verb} sent for ${boardKey}` });
        else store.toast({ kind: "error", text: `${verb} failed: ${await errorText(res)}` });
      } catch (err) {
        store.toast({ kind: "error", text: `${verb} failed: ${(err as Error).message}` });
      }
    },
  };
}

export type HubClient = ReturnType<typeof createHubClient>;
