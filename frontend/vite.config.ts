import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// /api/* goes to the FastAPI backend in development; in production the API is served
// from the same origin (reverse proxy) or VITE_API_BASE points at it.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: process.env.VITE_API_PROXY ?? 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
})
