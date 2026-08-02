import { resolve } from 'node:path'
import { defineConfig } from 'vitest/config'
import { playwright } from '@vitest/browser-playwright'

/**
 * PR #1977 review fix (P0 item 2, OWNER REQUEST_CHANGES) — AC4 requires
 * `tests/component/__screenshots__/` to be EXPLICIT in config. The plain
 * `test.browser.screenshotDirectory: '__screenshots__'` string form was
 * tried first, but Vitest 4.1.6 itself absolutizes any user-provided
 * `browser.screenshotDirectory` at config-resolve time
 * (`resolved.browser.screenshotDirectory = resolve(resolved.root,
 * resolved.browser.screenshotDirectory)` — see
 * `node_modules/vitest/dist/chunks/coverage.*.js`), while
 * `@vitest/browser`'s own default `resolveScreenshotPath` independently
 * re-joins that value with `root` again
 * (`join(root, config.browser.screenshotDirectory ?? "__screenshots__")` —
 * see `node_modules/@vitest/browser/dist/index.js`). Composing these two
 * behaviors double-prefixes the resolved path with `root`
 * (`<root>/tests/component/<root-without-leading-slash>/__screenshots__/...`),
 * which silently breaks reference-screenshot lookup — reproduced locally:
 * an explicit `screenshotDirectory: '__screenshots__'` string caused a
 * committed, otherwise-correct baseline to be reported as
 * "No existing reference screenshot found".
 *
 * To make AC4 genuinely explicit WITHOUT triggering that upstream
 * double-join, this config instead overrides
 * `browser.expect.toMatchScreenshot.resolveScreenshotPath` directly (an
 * explicit, fully-owned resolver — the alternative the OWNER review
 * explicitly allowed: "明示的な resolveScreenshotPath を実装する"),
 * bypassing `screenshotDirectory` entirely. `RESOLVED_SCREENSHOT_ROOT`
 * documents the resulting root in one place for both this config and
 * review readers; `scripts/check-visual-artifact-pipeline.py` derives the
 * same value independently from this file's source text (not a
 * self-referential constant — PR #1977 review fix, P0 item 2).
 */
const COMPONENT_SCREENSHOT_SUBDIR = '__screenshots__'

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
 *
 * PR #1977 review fix (P0 item 2, OWNER REQUEST_CHANGES): `include` is
 * intentionally restricted to a FLAT, non-recursive pattern
 * (`tests/component/*.vrt.test.ts`, not `tests/component/**\/*.vrt.test.ts`).
 * `scripts/check-visual-artifact-pipeline.py` derives each capture's
 * screenshot directory from `component_dir / screenshotDirectory /
 * specFilename`, which is only correct for specs directly under
 * `tests/component/`. A future spec nested under a subdirectory (e.g.
 * `tests/component/sub/foo.vrt.test.ts`) would resolve its Vitest default
 * screenshot path relative to that subdirectory, silently landing outside
 * both this audited root and the CI artifact upload path
 * (`tests/component/__screenshots__/`). Issue #1389's Current Validated
 * Scope is limited to the single `combat-hud-running` scenario directly
 * under `tests/component/`, so this restriction matches scope without
 * losing coverage; adding a nested spec later requires either lifting this
 * restriction together with a matching validator change, or keeping new
 * specs flat.
 *
 * A second, dedicated flat `include` entry backs the CI-only hidden-
 * attachment-upload negative control fixture (PR #1977 review fix, P0 item
 * 1) — `tests/component/__negative_control__/*.vrt.test.ts`. It is not a
 * production visual-baseline capture:
 * `scripts/check-visual-artifact-pipeline.py` explicitly recognises the
 * `__negative_control__` include entry as infra-only and excludes it from
 * the production capture / registry cross-validation (AC10). The fixture
 * itself is gated behind `VRT_NEGATIVE_CONTROL=true` so it never runs (and
 * never fails) during the normal `pnpm test:vrt:component` invocation.
 */
export default defineConfig({
  test: {
    include: [
      'tests/component/*.vrt.test.ts',
      'tests/component/__negative_control__/*.vrt.test.ts',
    ],
    browser: {
      enabled: true,
      headless: true,
      provider: playwright({
        // AC4: DPR pinned to 1 (device pixel ratio 1x) for deterministic
        // screenshot pixel dimensions across hosts/CI runners.
        contextOptions: {
          deviceScaleFactor: 1,
        },
        // AC6 (Issue #1389 fix_delta): explicit Chromium font-rendering /
        // color-profile determinism flags. Without these, local dev
        // environments (varying fontconfig / GPU rasterization paths) and
        // the actual GitHub Actions Ubuntu runner (software rendering,
        // container fontconfig) can rasterize text and colors slightly
        // differently, producing pixel mismatches above
        // `allowedMismatchedPixelRatio` even though nothing visually
        // meaningful changed. These flags pin font hinting/anti-aliasing
        // and color management so headless Chromium renders identically
        // regardless of host GPU/font stack.
        launchOptions: {
          args: [
            '--font-render-hinting=none',
            '--disable-lcd-text',
            '--force-color-profile=srgb',
            '--disable-gpu',
          ],
        },
      }),
      instances: [{ browser: 'chromium' }],
      viewport: {
        width: 1280,
        height: 720,
      },
      // AC1, AC4, AC6: committed provisional baseline root
      // (docs/dev/visual-baseline-registry.md). See the
      // `COMPONENT_SCREENSHOT_SUBDIR` comment above for why this is wired
      // via an explicit `resolveScreenshotPath` rather than the
      // `screenshotDirectory` shorthand.
      screenshotFailures: false,
      // AC4 (PR #1977 review fix, P0 item 2): explicit screenshot
      // directory, genuinely re-derivable by
      // `scripts/check-visual-artifact-pipeline.py` from this file's
      // source (not the Vitest Browser Mode default). With `include`
      // restricted to flat `tests/component/*.vrt.test.ts` specs (above),
      // `testFileDirectory` is always `tests/component`, so this always
      // resolves under `tests/component/__screenshots__/<specFilename>/`,
      // matching the committed baseline root and the
      // `tests/e2e/__screenshots__/` precedent.
      expect: {
        toMatchScreenshot: {
          // Mirrors `@vitest/browser`'s own default filename template
          // (`${arg}-${browserName}-${platform}${ext}` in
          // `node_modules/@vitest/browser/dist/index.js`) exactly, so the
          // committed baseline filename
          // (`combat-hud-running-chromium-linux.png`, generated inside the
          // Linux CI-pinned Docker container per AC6) is unchanged — only
          // the DIRECTORY resolution (root/testFileDirectory/screenshot
          // subdir/testFileName segment ordering) is taken over here to
          // avoid the upstream double-join bug documented above.
          resolveScreenshotPath: ({
            root,
            testFileDirectory,
            testFileName,
            arg,
            ext,
            browserName,
            platform,
          }) =>
            resolve(
              root,
              testFileDirectory,
              COMPONENT_SCREENSHOT_SUBDIR,
              testFileName,
              `${arg}-${browserName}-${platform}${ext}`,
            ),
        },
      },
    },
  },
})
