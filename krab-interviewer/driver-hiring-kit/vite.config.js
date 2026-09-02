import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Where this build will be SERVED from. Empty means the root, which is what
// driverinterviewcall.vercel.app has always been and still is. The tag site
// serves the very same app from https://tristatetags.com/drivers/, and a build
// for that needs its asset URLs and its router to agree about the prefix --
// so both come from this one value.
const BASE = process.env.VITE_BASE_PATH || "/";

export default defineConfig({
  base: BASE,
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
