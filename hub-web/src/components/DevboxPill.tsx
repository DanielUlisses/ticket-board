import type { Devbox, Usage } from "../protocol/types";
import { formatSeen } from "../columns";
import styles from "./DevboxPill.module.css";

export type HealthState = "ok" | "herdr-down" | "offline" | "refused" | "unknown";

/** What a box's sub-pill says about it, from its last devbox_status. */
export function healthOf(box: Devbox | undefined): { state: HealthState; label: string; detail: string } {
  if (!box) return { state: "unknown", label: "no status", detail: "The hub has not reported this box yet." };
  if (box.refused_reason) return { state: "refused", label: "refused", detail: box.refused_reason };
  const seen = box.last_seen ? `last seen ${formatSeen(box.last_seen)}` : "never seen";
  if (!box.online) return { state: "offline", label: "offline", detail: seen };
  if (box.health && !box.health.herdr.ok)
    return { state: "herdr-down", label: "herdr down", detail: `${box.health.herdr.status} — no launches on this box` };
  const h = box.health;
  return {
    state: "ok",
    label: h ? `${h.running} running` : "online",
    detail: h ? `daemon ${h.daemon_version} · ${h.herdr.status}` : seen,
  };
}

/** The usage meter's text and hover: `5h 42% · wk 72%`, reset times on hover. Unknown and stale are marked. */
export function usageMeter(usage: Usage | undefined): { state: Usage["state"]; text: string; detail: string } {
  const windows = [
    ["5h", usage?.five_hour],
    ["wk", usage?.seven_day],
  ] as const;
  const shown = windows.filter(([, w]) => w);
  if (!usage || usage.state === "unknown" || shown.length === 0)
    return { state: "unknown", text: "usage unknown", detail: "No Claude usage reported by this box yet." };
  const text = shown.map(([k, w]) => `${k} ${Math.round(w!.used_percentage)}%`).join(" · ");
  const resets = shown.map(([k, w]) =>
    Date.parse(w!.resets_at) <= Date.now() ? `${k} reset ${formatSeen(w!.resets_at)}` : `${k} resets ${formatSeen(w!.resets_at)}`,
  );
  const updated = usage.updated_at ? [`updated ${formatSeen(usage.updated_at)}`] : [];
  return usage.state === "stale"
    ? { state: "stale", text: `${text} (stale)`, detail: [...resets, ...updated, "older than 30 min while agents run"].join(" · ") }
    : { state: "ok", text, detail: [...resets, ...updated].join(" · ") };
}

/** The devbox sub-pill (or, `plain`, the swimlane's mono line): name and health. */
export function DevboxPill({ name, box, plain = false }: { name: string; box: Devbox | undefined; plain?: boolean }) {
  const health = healthOf(box);
  const meter = box?.online && box.health && !box.refused_reason ? usageMeter(box.health.usage) : null;
  return (
    <span className={plain ? styles.plain : styles.pill} data-health={health.state} title={health.detail}>
      <span className={styles.dot} aria-hidden="true" />
      <span>{name}</span>
      <span className={styles.label}>{health.label}</span>
      {meter && (
        <span className={styles.meter} data-usage={meter.state} title={meter.detail}>
          {meter.text}
        </span>
      )}
    </span>
  );
}
