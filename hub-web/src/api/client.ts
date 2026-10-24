// Typed client for the hub: `GET /api/boards` on start, `/ws/ui` kept open
// with backoff reconnects, `POST /api/scan` for ↻ Scan, and the ticket actions
// (`POST /api/boards/{key}/tickets/{nn}/action`, `.../frontier`): each accepted
// rpc is tracked in the store until its rpc_result arrives on the socket.
// Everything it learns goes into the one store; it holds no board data itself.
//
// ASSUMPTIONS (the hub routes arrive with ticket 08, unwritten when this was):
//   - GET /api/boards -> BoardsResponse `{boards, devboxes}`;
//   - POST /api/scan with `{"devbox": "<name>" | null}` (null: every box), any
//     2xx meaning the rescan was sent; the outcome arrives later as rpc_result;
//   - errors carry FastAPI's `{"detail": "..."}`.
import type {
  ActionResponse,
  ActivityEvent,
  ActivityResponse,
  DiffFileResponse,
  DiffListResponse,
  BoardsResponse,
  PolicyEdit,
  ReportResponse,
  SentRpc,
  TicketType,
  UiEvent,
} from "../protocol/types";
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

/** What a diff is taken against: the ticket's base sha (default) or its worktree's HEAD. */
export type DiffAgainst = "base" | "head";

/** The ticket RPCs with no params besides the ticket. */
export type ShipMethod = "open_pr" | "ssh_push" | "merge_pr" | "verify_landed" | "resolve_ticket";

/** What ▶ Launch runs with: the suggestions, or the unlocked selects. */
export interface LaunchSettings {
  type: TicketType;
  model: string;
  effort: string;
}

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

  /** POST a JSON body; true on 2xx, else a toast `<what> failed: <why>` and false. */
  async function postToast(url: string, body: unknown, what: string): Promise<boolean> {
    try {
      const res = await doFetch(url, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      if (res.ok) return true;
      store.toast({ kind: "error", text: `${what} failed: ${await errorText(res)}` });
    } catch (err) {
      store.toast({ kind: "error", text: `${what} failed: ${(err as Error).message}` });
    }
    return false;
  }

  // board_key is `<devbox>/<repo>/<board>`: encode each segment, keep the slashes.
  const boardUrl = (boardKey: string, leaf: string) =>
    `${origin}/api/boards/${boardKey.split("/").map(encodeURIComponent).join("/")}/${leaf}`;
  const ticketUrl = (boardKey: string, nn: string, leaf: string) =>
    boardUrl(boardKey, `tickets/${encodeURIComponent(nn)}/${leaf}`);

  async function post<T>(url: string, body?: unknown): Promise<T> {
    const res = await doFetch(url, {
      method: "POST",
      headers: { "content-type": "application/json", accept: "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!res.ok) throw new Error(await errorText(res));
    return (await res.json()) as T;
  }

  /** POST an action; its rpcs are tracked until their results, a refusal is a toast. */
  async function action(label: string, url: string, body?: unknown): Promise<ActionResponse | null> {
    try {
      const res = await post<ActionResponse>(url, body);
      store.track(res.rpcs);
      return res;
    } catch (err) {
      store.toast({ kind: "error", text: `${label} failed: ${(err as Error).message}` });
      return null;
    }
  }

  const ticketAction = (label: string, boardKey: string, nn: string, method: string, params: object) =>
    action(label, ticketUrl(boardKey, nn, "action"), { method, params });

  async function getJson<T>(url: string): Promise<T> {
    const res = await doFetch(url, { headers: { accept: "application/json" } });
    if (!res.ok) throw new Error(await errorText(res));
    return (await res.json()) as T;
  }

  /**
   * The ticket's uncommitted diff against its base: the file list, or with `file` that file's diff.
   * `against: "head"` diffs against the worktree's HEAD instead (a resolved merge, against the branch's previous head).
   */
  function diff(boardKey: string, nn: string, file?: undefined, against?: DiffAgainst): Promise<DiffListResponse>;
  function diff(boardKey: string, nn: string, file: string, against?: DiffAgainst): Promise<DiffFileResponse>;
  function diff(boardKey: string, nn: string, file?: string, against?: DiffAgainst) {
    const query = [
      ...(file === undefined ? [] : [`file=${encodeURIComponent(file)}`]),
      ...(against === "head" ? ["against=head"] : []),
    ];
    return getJson(ticketUrl(boardKey, nn, "diff") + (query.length ? `?${query.join("&")}` : ""));
  }

  return {
    /** The ticket's full report and raw body (ticket sheet's report modal). */
    report: (boardKey: string, nn: string) => getJson<ReportResponse>(ticketUrl(boardKey, nn, "report")),
    /**
     * ✦ Update via agy: a synthesis run on the ticket with the developer's comment
     * (empty: a plain refresh). Progress arrives as synthesis_* events, the result with
     * the board's next snapshot.
     */
    refine: (boardKey: string, nn: string, comment: string) =>
      postToast(ticketUrl(boardKey, nn, "comment"), { comment }, "Update"),
    /** Ticks (or unticks) a checklist item, matched by its text, in the box's hub-meta. */
    setTaskDone: (boardKey: string, nn: string, text: string, done: boolean) =>
      postToast(ticketUrl(boardKey, nn, "tasks"), { text, done }, "Checklist tick"),
    /** The ticket's uncommitted diff against its base: the file list, or with `file` that file's diff (read-only). */
    diff,
    /** The ticket's column moves, oldest first. */
    activity: async (boardKey: string, nn: string): Promise<ActivityEvent[]> =>
      (await getJson<ActivityResponse>(ticketUrl(boardKey, nn, "activity"))).events,
    /** ▶ Launch from the sheet: a manual launch (past max_parallel, with a warning). */
    async launch(boardKey: string, nn: string, settings: LaunchSettings): Promise<void> {
      await ticketAction("▶ Launch", boardKey, nn, "launch_ticket", { ...settings, manual: true });
    },
    /** ↵ Send: relays the message with tk.sh say; the rpc id, or null when refused. */
    async say(boardKey: string, nn: string, message: string): Promise<string | null> {
      const res = await ticketAction("↵ Send", boardKey, nn, "say_agent", { message });
      return res?.rpcs[0]?.id ?? null;
    },
    /** ⇡ Open PR, ⇡ Push via SSH, ✓ Merge, Verify or Resolve (landed: plain tk.sh resolve); true when the hub took it. Results arrive as rpc_results (merge: data.gates; verify: data.landed, data.open_pr). */
    async ship(label: string, boardKey: string, nn: string, method: ShipMethod): Promise<boolean> {
      return (await ticketAction(label, boardKey, nn, method, {})) !== null;
    },
    /** tk.sh resolve --no-code "<reason>": the reason is required. */
    async resolveNoCode(boardKey: string, nn: string, reason: string): Promise<boolean> {
      if (!reason.trim()) return false;
      return (await ticketAction("Resolve", boardKey, nn, "resolve_nocode", { reason: reason.trim() })) !== null;
    },
    /** Commit merge: merge-conflict.sh finish commits the resolved merge and pushes it; only after the sheet's confirm. */
    async finishConflict(boardKey: string, nn: string): Promise<boolean> {
      return (await ticketAction("Commit merge", boardKey, nn, "finish_conflict", { confirm: true })) !== null;
    },
    /** Abort merge: merge-conflict.sh abort puts the worktree back; the PR still conflicts. */
    async abortConflict(boardKey: string, nn: string): Promise<boolean> {
      return (await ticketAction("Abort merge", boardKey, nn, "abort_conflict", {})) !== null;
    },
    /** One poll of the agent's pane; its lines arrive as a pane_tail rpc_result. Rejects when not sent. */
    async paneTail(boardKey: string, nn: string, lines: number): Promise<void> {
      await post<ActionResponse>(ticketUrl(boardKey, nn, "action"), { method: "pane_tail", params: { lines } });
    },
    /** ▶ Frontier: every backlog ticket of the board, within the box's caps. */
    async frontier(boardKey: string): Promise<SentRpc[]> {
      const res = await action("▶ Frontier", boardUrl(boardKey, "frontier"));
      if (!res) return [];
      const skipped = (res.skipped ?? []).map((s) => `#${s.nn} (${s.reason})`);
      const launching =
        res.rpcs.length > 0
          ? `▶ Frontier: launching ${res.rpcs.map((r) => `#${r.nn}`).join(", ")}`
          : "▶ Frontier: nothing to launch";
      store.toast({
        kind: skipped.length > 0 ? "warn" : "ok",
        text: skipped.length > 0 ? `${launching}; skipped ${skipped.join(", ")}` : launching,
      });
      return res.rpcs;
    },
    /** 📄 Board's ↻ Refresh: a board-brief synthesis run now; true when the hub took it. The brief itself arrives with the board's next snapshot. */
    async refreshBrief(boardKey: string): Promise<boolean> {
      return (await action("Board brief", boardUrl(boardKey, "brief"))) !== null;
    },
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
    /** Writes the board's policy override (tk.sh policy-set); the new resolved policy arrives with its next snapshot. */
    async setPolicy(boardKey: string, values: PolicyEdit): Promise<boolean> {
      try {
        const res = await doFetch(`${origin}/api/boards/${boardKey.split("/").map(encodeURIComponent).join("/")}/policy`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ values }),
        });
        if (res.ok) return true;
        store.toast({ kind: "error", text: `Policy not saved: ${await errorText(res)}` });
      } catch (err) {
        store.toast({ kind: "error", text: `Policy not saved: ${(err as Error).message}` });
      }
      return false;
    },
    /** Proposes a project-memory rule: the board's daemon appends a ticket for it (the hub never writes project-memory.md). */
    async proposeMemory(boardKey: string, text: string): Promise<boolean> {
      try {
        const res = await doFetch(`${origin}/api/boards/${boardKey.split("/").map(encodeURIComponent).join("/")}/memory`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ text }),
        });
        if (res.ok) {
          store.toast({ kind: "ok", text: "Memory ticket proposed." });
          return true;
        }
        store.toast({ kind: "error", text: `Memory not proposed: ${await errorText(res)}` });
      } catch (err) {
        store.toast({ kind: "error", text: `Memory not proposed: ${(err as Error).message}` });
      }
      return false;
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
    /** ⟳ Sync box: the hub asks the box's daemon to start `devbox sync`; a refusal (tickets working or in review, a sync already running, systemd-run failing: its stderr) comes back as its rpc_result, which the store toasts. */
    async syncBox(devbox: string, force: boolean) {
      try {
        const res = await doFetch(`${origin}/api/devboxes/${encodeURIComponent(devbox)}/sync`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ force }),
        });
        if (res.ok) store.toast({ kind: "ok", text: `⟳ Sync sent to ${devbox}` });
        else store.toast({ kind: "error", text: `⟳ Sync failed: ${await errorText(res)}` });
      } catch (err) {
        store.toast({ kind: "error", text: `⟳ Sync failed: ${(err as Error).message}` });
      }
    },
  };
}

export type HubClient = ReturnType<typeof createHubClient>;
