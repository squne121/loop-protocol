import { defineConfig, devices } from '@playwright/test'

/**
 * Playwright E2E configuration for LOOP_PROTOCOL.
 *
 * - Uses Vite preview server (pnpm build && pnpm preview) — not the dev server.
 * - VITE_E2E_MODE=true enables the read-only window.__LOOP_E2E__ observability hook.
 * - trace: 'retain-on-failure' ensures trace files are saved on any first failure
 *   (AC6: first-run failures are captured without requiring retries).
 */
export default defineConfig({
  testDir: './tests/e2e',
  /* Run tests in files in parallel */
  fullyParallel: false,
  /* Fail the build on CI if you accidentally left test.only in the source code. */
  forbidOnly: !!process.env.CI,
  /* Retry on CI only */
  retries: process.env.CI ? 1 : 0,
  /* One worker for consistent simulation timing */
  workers: 1,
  /* Reporter to use. */
  reporter: [['html', { open: 'never' }], ['list']],
  use: {
    /* Base URL — matches the preview server port */
    baseURL: 'http://127.0.0.1:4173',
    /* Collect trace on failure (AC6) */
    trace: 'retain-on-failure',
    /* Screenshot on failure */
    screenshot: 'only-on-failure',
    /* Viewport matches default arena */
    viewport: { width: 1280, height: 720 },
  },

  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],

  /* Start the Vite preview server before running tests.
   * Uses preview (not dev) so the build is deterministic.
   * VITE_E2E_MODE=true enables the read-only __LOOP_E2E__ hook. */
  webServer: {
    command:
      'VITE_E2E_MODE=true pnpm build && VITE_E2E_MODE=true pnpm preview -- --host 127.0.0.1 --port 4173 --strictPort',
    url: 'http://127.0.0.1:4173',
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    stdout: 'pipe',
    stderr: 'pipe',
  },
})
