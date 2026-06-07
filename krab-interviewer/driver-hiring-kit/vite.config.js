import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: "https://krab-interviewer-bot-j5dv.onrender.com",
        changeOrigin: true,
        secure: true,
      },
    },
  },
});
