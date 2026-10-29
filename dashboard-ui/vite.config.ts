import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The built page is served by `healops dashboard` from src/healops/dashboard/static.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "../src/healops/dashboard/static",
    emptyOutDir: true,
    assetsInlineLimit: 0,
    sourcemap: false,
  },
  server: { proxy: { "/api": "http://127.0.0.1:8765" } },
});
