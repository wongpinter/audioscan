import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { fileURLToPath } from 'node:url'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: '/static/',
  build: {
    outDir: fileURLToPath(new URL('./src/audioscan/static', import.meta.url)),
    emptyOutDir: true,
  },
  server: { proxy: { '/api': 'http://127.0.0.1:8111', '/auth': 'http://127.0.0.1:8111' } },
})
