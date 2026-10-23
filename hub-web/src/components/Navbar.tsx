import type { Page } from "../store/store";
import styles from "./Navbar.module.css";
import { useStore, useView } from "./StoreContext";

/** Brand, the Cards | Live Board switch, ↻ Scan and the ☾ Night placeholder. */
export function Navbar({ onScan }: { onScan: (devbox: string | null) => void }) {
  const store = useStore();
  const { page, filter } = useView();
  const pageButton = (p: Page, label: string, dot = false) => (
    <button
      type="button"
      className={styles.pageBtn}
      aria-pressed={page === p}
      onClick={() => store.setPage(p)}
    >
      {dot && <span className={styles.dot} aria-hidden="true" />}
      {label}
    </button>
  );
  return (
    <nav className={styles.navbar}>
      <span className={styles.left}>
        <span className={styles.brand}>
          <span className={styles.brandGlyph}>-{"{|}"}-</span> tk-hub
        </span>
        <span className={styles.pageSwitch}>
          {pageButton("cards", "Cards")}
          {pageButton("live", "Live Board", true)}
        </span>
      </span>
      <span className={styles.right}>
        <button
          type="button"
          className={styles.btnPill}
          title="Trigger tk-daemon to rescan ~/work/*/.scratch"
          onClick={() => onScan(filter.devbox)}
        >
          ↻ Scan
        </button>
        <button
          type="button"
          className={`${styles.btnPill} ${styles.primary}`}
          disabled
          title="Night shift arrives with the scheduler"
        >
          ☾ Night
        </button>
      </span>
    </nav>
  );
}
