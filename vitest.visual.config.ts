import { defineConfig } from 'vitest/config'
import { playwright } from '@vitest/browser-playwright'

/**
 * vitest.visual.config.ts — component VRT config (Issue #1389).
 *
 * Vitest Browser Mode config, entirely separate from `vite.config.ts` /
 * the default `pnpm test` Vitest config. Only ever invoked explicitly via
 * `--config vitest.visual.config.ts` (`pnpm test:vrt:component` /
 * `pnpm test:vrt:update:component`), so this file is never picked up by
 * plain `vitest run` (`pnpm test`), and `pnpm test` additionally excludes
 * `tests/component/**` (AC2) so the two suites never collide during file
 * discovery.
 *
 * Report-only lane (Issue #1389 Outcome / Scope Delta): this suite backs
 * the non-required `component-vrt-report` CI job only. `component-vrt`
 * baseline maturity is `provisional`
 * (docs/dev/visual-baseline-registry.md) and this suite is never wired
 * into `pnpm test`/branch protection required checks.
 *
 * AC4: Chromium, headless, viewport 1280x720, DPR 1, screenshot directory
 * are all explicit below rather than left to Vitest Browser Mode defaults.
 */
export default defineConfig({
  test: {
    include: ['tests/component/**/*.vrt.test.ts'],
    browser: {
      enabled: true,
      headless: true,
      provider: playwright({
        // AC4: DPR pinned to 1 (device pixel ratio 1x) for deterministic
        // screenshot pixel dimensions across hosts/CI runners.
        contextOptions: {
          deviceScaleFactor: 1,
        },
      }),
      instances: [{ browser: 'chromium' }],
      viewport: {
        width: 1280,
        height: 720,
      },
      // AC1, AC4, AC6: committed provisional baseline root
      // (docs/dev/visual-baseline-registry.md). `screenshotDirectory` is
      // intentionally left unset — Vitest Browser Mode's documented
      // default already resolves it to `__screenshots__` next to each test
      // file (i.e. `tests/component/__screenshots__/`, matching the
      // `tests/e2e/__screenshots__/` precedent). Explicitly setting this
      // option instead resolves it relative to the project root and would
      // move the baseline root away from `tests/component/`.
      screenshotFailures: false,
    },
  },
})
