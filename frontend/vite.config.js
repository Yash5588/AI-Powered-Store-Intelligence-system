import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server on :3000 to match the CORS allow-list and docker-compose mapping.
export default defineConfig({
  plugins: [react()],
  server: { host: true, port: 3000 },
  preview: { host: true, port: 3000 },
});
