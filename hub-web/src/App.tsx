import { useEffect } from "react";
import type { HubClient } from "./api/client";
import styles from "./App.module.css";
import { CardGrid } from "./components/CardGrid";
import { Controls } from "./components/Controls";
import { LiveBoard } from "./components/LiveBoard";
import { Navbar } from "./components/Navbar";
import { StoreProvider, useView } from "./components/StoreContext";
import { TicketSheet } from "./components/TicketSheet";
import { Toasts } from "./components/Toasts";
import type { Store } from "./store/store";

function Shell({ client }: { client: HubClient }) {
  const { page, counts } = useView();
  const needsYou = counts.attention; // under the Devbox / Board filters
  useEffect(() => {
    document.title = needsYou > 0 ? `(${needsYou}) tk-hub` : "tk-hub";
  }, [needsYou]);
  return (
    <>
      <Navbar onScan={(devbox) => void client.scan(devbox)} scheduler={client} />
      <Controls onArchive={(key, archived) => void client.setArchived(key, archived)} />
      <main className={styles.main}>{page === "cards" ? <CardGrid client={client} onFrontier={(key) => client.frontier(key)} /> : <LiveBoard />}</main>
      <TicketSheet client={client} />
      <Toasts />
    </>
  );
}

/** The hub-web frame: everything reads the one store, fed by the hub client. */
export function App({ store, client }: { store: Store; client: HubClient }) {
  useEffect(() => {
    void client.start();
    return () => client.stop();
  }, [client]);
  return (
    <StoreProvider store={store}>
      <Shell client={client} />
    </StoreProvider>
  );
}
