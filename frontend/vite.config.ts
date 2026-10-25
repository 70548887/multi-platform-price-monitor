import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 28672,
    strictPort: true,
    proxy: { '/api': { target: 'http://127.0.0.1:28671', changeOrigin: false } },
  },
  preview: { port: 28672, strictPort: true },
  build: { chunkSizeWarningLimit: 1200 },
})
