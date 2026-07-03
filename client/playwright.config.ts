import { defineConfig, devices } from '@playwright/test'

// E2e runs against the Vite dev server with /api mocked at the browser level
// (page.route), so no backend or Jandy account is needed. The primary project
// is a mobile viewport — this app is used on phones at the pool.
export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? 'github' : 'list',
  use: {
    // "localhost", not 127.0.0.1: on Windows the Vite dev server binds ::1.
    baseURL: 'http://localhost:5173',
    trace: 'on-first-retry',
  },
  projects: [
    { name: 'mobile', use: { ...devices['Pixel 7'] } },
    { name: 'desktop', use: { ...devices['Desktop Chrome'] } },
  ],
  webServer: {
    command: 'npm run dev -- --strictPort',
    url: 'http://localhost:5173',
    reuseExistingServer: !process.env.CI,
  },
})
