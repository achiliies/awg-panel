import { fileURLToPath, URL } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  // Relative asset URLs: the panel is mounted under a random secret base path
  // (/ab12cd34/) that is only known at install time, so nothing may be
  // hard-coded to the server root.
  base: "./",
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // Installers ship dist/ as a release asset; source maps would double its
    // size and leak nothing useful to an operator.
    sourcemap: false,
    chunkSizeWarningLimit: 900,
  },
  server: {
    port: 5173,
    strictPort: false,
    proxy: {
      // Django dev server, on the port `make dev` was given. The default is the
      // one the systemd unit uses in production; a second panel run out of a
      // worktree is started on another one, and hard-coding 8088 here proxied
      // its API calls into whichever panel already owned that port.
      "/api": {
        target: `http://127.0.0.1:${process.env.API_PORT || "8088"}`,
        changeOrigin: false,
      },
    },
  },
  preview: {
    port: 4173,
  },
});
