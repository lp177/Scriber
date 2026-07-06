import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// Vite configuration for the Scriber admin dashboard.
// In dev mode, API calls are proxied to the Python backend on port 8080.
export default defineConfig({
  plugins: [vue()],
  server: {
    proxy: {
      "/api": "http://localhost:8080",
    },
  },
});
