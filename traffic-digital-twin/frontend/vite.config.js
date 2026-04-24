import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // /ws 요청을 백엔드로 프록시 (CORS 없이 WS 연결)
      "/ws": {
        target: "ws://localhost:8000",
        ws: true,
      },
    },
  },
  optimizeDeps: {
    include: [
      "react",
      "react-dom",
      "deck.gl",
      "@deck.gl/react",
      "@deck.gl/layers",
      "react-map-gl",
      "recharts",
    ],
  },
});
