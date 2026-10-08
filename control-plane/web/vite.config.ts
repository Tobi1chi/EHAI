import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The page talks to ehai-manager same-origin; in development Vite forwards the API paths.
const manager = process.env.EHAI_MANAGER_URL ?? "http://127.0.0.1:8765";

export default defineConfig({
  base: "/ui/",
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true },
  server: {
    proxy: {
      "/api": manager,
      "/workspaces": manager,
    },
  },
});
