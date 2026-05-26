import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import { fileURLToPath, URL } from "node:url";

// The SPA lives at /dashboard/ on the production host, so every asset
// URL must be prefixed accordingly. In dev mode we serve at root with
// a proxy for /api so devtools-style requests reach the real API.
export default defineConfig({
  base: "/dashboard/",
  plugins: [vue()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
    // Hashed chunks land in dist/assets/. FastAPI mounts that exact path
    // at /dashboard/assets/ — see apps/api/main.py.
    assetsDir: "assets",
  },
});
