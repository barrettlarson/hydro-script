/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { VitePWA } from 'vite-plugin-pwa'

// Dev: Vite proxies /api to the FastAPI dev server, so the browser only ever
// talks to one origin.
// Prod: FastAPI serves the built client from the same origin (see main.py).
export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: 'autoUpdate',
      includeAssets: ['favicon.svg', 'apple-touch-icon.png'],
      manifest: {
        name: 'Hydro — Pool & Spa',
        short_name: 'Hydro',
        description: 'Pool and spa control',
        theme_color: '#0b2447',
        background_color: '#0b2447',
        display: 'standalone',
        icons: [
          { src: 'pwa-192.png', sizes: '192x192', type: 'image/png' },
          { src: 'pwa-512.png', sizes: '512x512', type: 'image/png' },
          { src: 'pwa-512.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' },
        ],
      },
      workbox: {
        // The app shell is precached for instant launch, but /api must never
        // be served by the service worker — status has to be live.
        navigateFallbackDenylist: [/^\/api\//],
      },
    }),
  ],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: './src/test/setup.ts',
    // e2e specs run under Playwright, not vitest
    exclude: ['e2e/**', 'node_modules/**'],
  },
})