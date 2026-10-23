import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./styles/tokens.css";
import { createHubClient } from "./api/client";
import { App } from "./App";
import { createStore } from "./store/store";

const store = createStore();
const client = createHubClient(store);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App store={store} client={client} />
  </StrictMode>,
);
