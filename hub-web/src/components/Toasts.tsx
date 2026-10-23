import { useEffect } from "react";
import type { Toast } from "../store/store";
import { useStore, useView } from "./StoreContext";
import styles from "./Toasts.module.css";

/** How long a toast stays (prototype: auto-hide 2800 ms). */
export const TOAST_MS = 2800;

function ToastItem({ toast }: { toast: Toast }) {
  const store = useStore();
  useEffect(() => {
    const timer = setTimeout(() => store.dismiss(toast.id), TOAST_MS);
    return () => clearTimeout(timer);
  }, [store, toast.id]);
  return (
    <div role="status" className={styles.toast} data-kind={toast.kind}>
      {toast.text}
    </div>
  );
}

/** Toasts: ↻ Scan outcomes and every rpc_result from the hub. */
export function Toasts() {
  const { toasts } = useView();
  return (
    <div className={styles.stack}>
      {toasts.map((t) => (
        <ToastItem key={t.id} toast={t} />
      ))}
    </div>
  );
}
