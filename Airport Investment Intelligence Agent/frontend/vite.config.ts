import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Development: Vite serves the UI on :5173 and proxies /api to FastAPI on :8000, so the browser
// sees one origin and the session cookie stays first-party. Production: FastAPI serves `dist/`.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000" },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
