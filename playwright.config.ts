import { defineConfig, devices } from '@playwright/test'

/**
 * Playwright E2E configuration for LOOP_PROTOCOL.
 *
 * - Uses Vite preview server (started by webServer) — not the dev server.
 *   Run `pnpm test:e2e` for local (includes build), `pnpm test:e2e:ci` for CI (build done separately).
 * - VITE_E2E_MODE=true enables the read-only window.__LOOP_E2E__ observability hook.
 * - trace: 'retain-on-failure' ensures trace files are saved on any first failure
 *   (AC6: first-run failures are captured without requiring retries).
 *
 * Preview-namespace dedicated lane (Issue #1283 / PR #1517 review fix, P0
 * Blocker 1): `tests/e2e/m4-preview-namespace.spec.ts` asserts build-time
 * `VITE_LOOP_STORAGE_NAMESPACE` isolation against a production-like build
 * (no `VITE_E2E_MODE`). Running it in the same lane as the standard
 * `VITE_E2E_MODE=true`-only E2E suite let it silently pass via a fallback
 * that has since been removed from the spec itself — the spec now throws if
 * required env is missing instead. To prevent it from ever running against
 * the wrong build, it is EXCLUDED from the default test run here
 * (`testIgnore`) and is ONLY included when `LOOP_E2E_PREVIEW_NAMESPACE_LANE`
 * is `true` (`testMatch`), which also forces `reuseExistingServer: false` so
 * a stale server from a different worktree/build/PR cannot be reused. See
 * `pnpm run test:e2e:preview-namespace` in package.json.
 *
 * VRT lane (Issue #1386 PR #1721 review fix, P1 Blocker 4): `LOOP_VRT_LANE=true`
 * (set by the `test:vrt` / `test:vrt:e2e` package scripts) forces
 * `reuseExistingServer: false` for the same reason as the preview-namespace
 * lane above — `pnpm test:vrt` rebuilds `dist/` with `VITE_E2E_MODE=true`
 * baked in at build time (Vite's `import.meta.env` is a build-time static
 * replacement, so setting the env var only on the `webServer.command`
 * itself is NOT sufficient once a server is already running), and a stale
 * preview server left over from a different worktree/commit/build (with or
 * without `VITE_E2E_MODE`) must never be silently reused for a VRT
 * comparison.
 *
 * Dedicated `outputDir` per lane (Issue #1387 / PR #1813 review fix, P1
 * Blocker 3): Playwright's default `outputDir` is `test-results/` for EVERY
 * lane. In `.github/workflows/ci.yml`'s `e2e` job, the standard lane runs
 * first (uploading `test-results/` as VRT evidence), and the AC9
 * preview-namespace lane runs afterwards IN THE SAME JOB — `pnpm exec
 * playwright test` always cleans its `outputDir` at the start of a run, so
 * without this split the preview-namespace lane's run silently overwrites
 * the standard lane's already-uploaded `test-results/` contents before the
 * job's "Summarize visual regression evidence" step reads that directory,
 * making its actual/expected/diff detection reflect the WRONG lane. Giving
 * the preview-namespace lane its own `test-results-preview-namespace/`
 * output directory means the standard lane's `test-results/` is never
 * touched again after that lane finishes.
 *
 * Nested base (deploy-pr-equivalent) preview verification (Issue #1283
 * 2026-08-03 OWNER review repair, AC9/AC15): the preview-namespace lane
 * build already honors `VITE_BASE_PATH` via `vite.config.ts` (Vite bakes
 * `base` into every emitted asset URL and `vite preview` only serves the
 * app under that base). `NESTED_BASE_PATH` here normalizes the SAME
 * `VITE_BASE_PATH` env var with the identical rule as `vite.config.ts`
 * (default `/` when unset), so `baseURL`/`webServer.url` navigate to the
 * exact nested prefix instead of root — a root `page.goto('/')` against a
 * nested build would 404 and silently prove nothing about nested-base
 * behavior.
 */

function normalizeBasePathForE2E(raw: string | undefined): string {
  if (!raw || raw.trim() === '') return '/'

  const value = raw.trim()
  if (!value.startsWith('/') || value.startsWith('//')) {
    throw new Error(`VITE_BASE_PATH must start with a single "/": ${value}`)
  }

  return value.endsWith('/') ? value : `${value}/`
}

const PREVIEW_NAMESPACE_LANE = process.env.LOOP_E2E_PREVIEW_NAMESPACE_LANE === 'true'
const PREVIEW_NAMESPACE_SPEC = '**/m4-preview-namespace.spec.ts'
const VRT_LANE = process.env.LOOP_VRT_LANE === 'true'
const NESTED_BASE_PATH = normalizeBasePathForE2E(process.env.VITE_BASE_PATH)
const PREVIEW_ORIGIN = 'http://127.0.0.1:4173'
const PREVIEW_BASE_URL = PREVIEW_NAMESPACE_LANE ? `${PREVIEW_ORIGIN}${NESTED_BASE_PATH}` : PREVIEW_ORIGIN

// ---------------------------------------------------------------------------
// Exclusive E2E lane selector (Issue #2119 AC14): `LOOP_E2E_LANE` partitions
// the standard E2E suite into `core` (everything except the responsive
// matrix and the dedicated preview-namespace spec) and `responsive` (ONLY
// `assist-player-affordance-responsive.spec.ts`). `preview-namespace` is
// recognized as a third enum member for a complete, exclusive contract, but
// its OWN test-selection semantics remain governed by the pre-existing
// `LOOP_E2E_PREVIEW_NAMESPACE_LANE` boolean (Out of Scope: this Issue does
// not change the preview-namespace lane's production-like build/storage
// isolation contract) -- `LOOP_E2E_LANE` only adds a fail-closed consistency
// check against that legacy flag so the two selectors can never silently
// disagree. Unknown values and comma-separated multi-lane values are
// REJECTED (fail-closed), not silently defaulted.
// ---------------------------------------------------------------------------
const RESPONSIVE_MATRIX_SPEC = '**/assist-player-affordance-responsive.spec.ts'
const VALID_E2E_LANES = ['core', 'responsive', 'preview-namespace'] as const
type E2ELane = (typeof VALID_E2E_LANES)[number]

function resolveE2ELane(raw: string | undefined): E2ELane {
  if (raw === undefined || raw === '') return 'core'
  if (raw.includes(',') || !(VALID_E2E_LANES as readonly string[]).includes(raw)) {
    throw new Error(
      `LOOP_E2E_LANE must be exactly one of ${VALID_E2E_LANES.join('|')} `
        + `(fail-closed on multi-lane or unknown lane values), got: "${raw}"`,
    )
  }
  return raw as E2ELane
}

const E2E_LANE = resolveE2ELane(process.env.LOOP_E2E_LANE)

if (process.env.LOOP_E2E_LANE !== undefined) {
  if (E2E_LANE === 'preview-namespace' && !PREVIEW_NAMESPACE_LANE) {
    throw new Error(
      'LOOP_E2E_LANE=preview-namespace requires LOOP_E2E_PREVIEW_NAMESPACE_LANE=true (lane selector inconsistency)',
    )
  }
  if (E2E_LANE !== 'preview-namespace' && PREVIEW_NAMESPACE_LANE) {
    throw new Error(
      `LOOP_E2E_PREVIEW_NAMESPACE_LANE=true requires LOOP_E2E_LANE=preview-namespace, got "${E2E_LANE}" (lane selector inconsistency)`,
    )
  }
  if (E2E_LANE === 'responsive' && VRT_LANE) {
    throw new Error('LOOP_E2E_LANE=responsive is incompatible with LOOP_VRT_LANE=true')
  }
}

export default defineConfig({
  testDir: './tests/e2e',
  // Exclusive lane contract (Issue #2119 AC14): exactly one of
  // preview-namespace / responsive / core selects tests at a time, never a
  // combination.
  ...(PREVIEW_NAMESPACE_LANE
    ? { testMatch: [PREVIEW_NAMESPACE_SPEC] }
    : E2E_LANE === 'responsive'
      ? { testMatch: [RESPONSIVE_MATRIX_SPEC] }
      : { testIgnore: [PREVIEW_NAMESPACE_SPEC, RESPONSIVE_MATRIX_SPEC] }),
  /* Run tests in files in parallel */
  fullyParallel: false,
  /* Fail the build on CI if you accidentally left test.only in the source code. */
  forbidOnly: !!process.env.CI,
  /* Retry on CI only */
  retries: process.env.CI ? 1 : 0,
  /* Required CI is read-only. Local runs retain Playwright's normal missing
   * snapshot behaviour, while the explicit test:vrt:update:e2e script is the
   * only candidate-generation entry point. */
  updateSnapshots: process.env.CI ? 'none' : 'missing',
  /* One worker for consistent simulation timing */
  workers: 1,
  /* Reporter to use.
   * Preview-namespace lane writes its HTML report to a distinct output
   * folder (not the default `playwright-report/`) so its CI upload step
   * cannot collide with `scripts/check-visual-artifact-pipeline.py`'s fixed
   * id/name contract for the `playwright-report/` path (PR #1517 review
   * fix). */
  reporter: [
    ['html', {
      open: 'never',
      outputFolder: PREVIEW_NAMESPACE_LANE
        ? 'playwright-report-preview-namespace'
        : E2E_LANE === 'responsive'
          ? 'playwright-report-e2e-responsive-matrix'
          : 'playwright-report',
    }],
    ['list'],
  ],
  use: {
    /* Base URL — matches the preview server port. Preview-namespace lane
     * (Issue #1283 AC9/AC15) points at the nested `VITE_BASE_PATH` prefix
     * so `page.goto('/')` (relative to `baseURL`) lands on the exact
     * deploy-pr-equivalent nested path, not root. */
    baseURL: PREVIEW_BASE_URL,
    /* Collect trace on failure (AC6) */
    trace: 'retain-on-failure',
    /* Screenshot on failure */
    screenshot: 'only-on-failure',
    /* Viewport matches default arena */
    viewport: { width: 1280, height: 720 },
  },

  /* Deterministic location for visual baselines generated by toHaveScreenshot(). */
  snapshotPathTemplate: '{testDir}/__screenshots__/{testFilePath}/{arg}{ext}',

  /* Lane-specific outputDir (Issue #1387 / PR #1813 review fix, P1 Blocker
   * 3, see file header) — the preview-namespace lane must never overwrite
   * the standard lane's `test-results/` VRT evidence directory. */
  outputDir: PREVIEW_NAMESPACE_LANE
    ? 'test-results-preview-namespace'
    : E2E_LANE === 'responsive'
      ? 'test-results-e2e-responsive-matrix'
      : 'test-results',

  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],

  /* Start the Vite preview server before running tests.
   * Uses preview (not dev) so the build is deterministic.
   * Standard lane: VITE_E2E_MODE=true enables the read-only __LOOP_E2E__ hook.
   * Preview-namespace lane: no VITE_E2E_MODE (production-like build/serve —
   * see file header); reuseExistingServer is always false so a stale server
   * from a different worktree/build/PR is never reused (P0 Blocker 1, item 5). */
  webServer: {
    command: PREVIEW_NAMESPACE_LANE
      ? 'pnpm exec vite preview --host 127.0.0.1 --port 4173 --strictPort'
      : 'VITE_E2E_MODE=true pnpm exec vite preview --host 127.0.0.1 --port 4173 --strictPort',
    /* Preview-namespace lane readiness probe must target the nested base
     * path too (Issue #1283 AC9) — `vite preview` with a non-default
     * `base` does not serve the app at root, so probing root would never
     * become ready and the webServer startup would time out. */
    url: PREVIEW_BASE_URL,
    reuseExistingServer: PREVIEW_NAMESPACE_LANE || VRT_LANE ? false : !process.env.CI,
    timeout: 120_000,
    stdout: 'pipe',
    stderr: 'pipe',
  },
})
