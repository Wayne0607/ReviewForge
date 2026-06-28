import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../backend/src/reviewforge/static',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:8000',
      '/webhook': 'http://localhost:8000',
      '/health': 'http://localhost:8000',
    },
  },
})
