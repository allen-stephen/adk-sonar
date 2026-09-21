import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const BACKEND_URL = process.env.VITE_BACKEND_URL || "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    host: "0.0.0.0",
    proxy: {
      "/api": {
        target: BACKEND_URL,
        changeOrigin: true,
        headers: {
          Origin: BACKEND_URL,
        },
      },
      "/apps": {
        target: BACKEND_URL,
        changeOrigin: true,
        headers: {
          Origin: BACKEND_URL,
        },
      },
      "/list-apps": {
        target: BACKEND_URL,
        changeOrigin: true,
        headers: {
          Origin: BACKEND_URL,
        },
      },
      "/run_live": {
        target: BACKEND_URL.replace(/^http/, "ws"),
        ws: true,
        changeOrigin: true,
        headers: {
          Origin: BACKEND_URL,
        },
      },
    },
  },
});
