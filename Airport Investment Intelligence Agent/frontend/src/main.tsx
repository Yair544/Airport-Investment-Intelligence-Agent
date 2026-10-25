import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createApiClient, type FetchLike } from "./api/client";
import { App } from "./App";
import "./styles.css";

async function start(): Promise<void> {
  let fetchImpl: FetchLike | undefined;
  // Development only: `?demo` swaps the network for an in-memory mock of the API. The whole branch
  // is removed from production builds because import.meta.env.DEV is statically false there.
  if (import.meta.env.DEV && new URLSearchParams(window.location.search).has("demo")) {
    const { createMockFetch } = await import("./dev/mockApi");
    fetchImpl = createMockFetch();
  }
  const root = document.getElementById("root");
  if (!root) throw new Error("Missing #root element");
  createRoot(root).render(
    <StrictMode>
      <App api={createApiClient(fetchImpl)} demo={fetchImpl !== undefined} />
    </StrictMode>,
  );
}

void start();
