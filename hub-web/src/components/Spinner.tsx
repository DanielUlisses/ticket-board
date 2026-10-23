import styles from "./Spinner.module.css";

/** A small spinner for a button whose action waits on its rpc_result. */
export function Spinner() {
  return <span className={styles.spinner} aria-hidden="true" />;
}
