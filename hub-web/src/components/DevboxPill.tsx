import type { Devbox } from "../protocol/types";
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

/** The devbox sub-pill (or, `plain`, the swimlane's mono line): name and health. */
export function DevboxPill({ name, box, plain = false }: { name: string; box: Devbox | undefined; plain?: boolean }) {
  const health = healthOf(box);
  return (
    <span className={plain ? styles.plain : styles.pill} data-health={health.state} title={health.detail}>
      <span className={styles.dot} aria-hidden="true" />
      <span>{name}</span>
      <span className={styles.label}>{health.label}</span>
    </span>
  );
}
