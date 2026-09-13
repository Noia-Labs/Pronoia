import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 端口可通过环境变量覆盖（start_local.sh 用），默认保持原行为 5173 / 8000
const FRONTEND_PORT = Number(process.env.FEVER_FRONTEND_PORT || 5173);
const BACKEND_PORT = Number(process.env.FEVER_BACKEND_PORT || 8000);

export default defineConfig({
  plugins: [react()],
  server: {
    port: FRONTEND_PORT,
    proxy: {
      "/api": {
        target: `http://localhost:${BACKEND_PORT}`,
        changeOrigin: true,
        // SSE / 流式响应不缓冲，确保 EventSource 实时收到数据
        configure: (proxy) => {
          proxy.on("proxyReq", (proxyReq) => {
            proxyReq.setHeader("Accept", "text/event-stream, */*");
          });
        },
      },
    },
  },
  build: {
    chunkSizeWarningLimit: 700,
    rollupOptions: {
      output: {
        manualChunks: {
          echarts: ["echarts/core", "echarts/charts", "echarts/components", "echarts/renderers"],
          markdown: ["react-markdown", "remark-gfm"],
          react: ["react", "react-dom"],
        },
      },
    },
  },
});
