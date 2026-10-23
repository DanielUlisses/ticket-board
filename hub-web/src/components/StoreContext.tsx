import { createContext, useContext, useSyncExternalStore, type ReactNode } from "react";
import type { Store, View } from "../store/store";

const StoreContext = createContext<Store | null>(null);

export function StoreProvider({ store, children }: { store: Store; children: ReactNode }) {
  return <StoreContext.Provider value={store}>{children}</StoreContext.Provider>;
}

export function useStore(): Store {
  const store = useContext(StoreContext);
  if (!store) throw new Error("useStore outside <StoreProvider>");
  return store;
}

/** The store's current view; re-renders on every store change. */
export function useView(): View {
  const store = useStore();
  return useSyncExternalStore(store.subscribe, store.view);
}
