import { expect, test } from 'vitest'
import { page } from 'vitest/browser'

/**
 * hidden-attachment-negative-control.vrt.test.ts — deliberate, controlled
 * visual-mismatch fixture (PR #1977 review fix, P0 item 1, OWNER
 * REQUEST_CHANGES).
 *
 * Purpose: prove END-TO-END that `.vitest-attachments/tests/component/**`
 * (a hidden, dot-prefixed directory) is actually retrievable via the
 * `component-vrt-negative-control-attachments` `actions/upload-artifact@v6`
 * step in `.github/workflows/ci.yml` — not merely that the workflow YAML
 * *looks* correct (`actions/upload-artifact` excludes hidden files/
 * directories by default unless `include-hidden-files: true` is set).
 *
 * This is a throwaway/controlled test double, isolated from the real
 * `combat-hud-running` production capture and its baseline root:
 * - it lives in `tests/component/__negative_control__/`, a dedicated
 *   `include` entry in `vitest.visual.config.ts` that
 *   `scripts/check-visual-artifact-pipeline.py` explicitly recognises as
 *   infra-only and excludes from the production capture / registry
 *   cross-validation (it is not a real visual baseline contract);
 * - its committed reference PNG lives under its own
 *   `tests/component/__negative_control__/__screenshots__/` subtree,
 *   never mixed with `tests/component/__screenshots__/`.
 *
 * The reference PNG is a solid magenta 2x2 pixel image; this test captures
 * a solid green 2x2 element and compares it with `allowedMismatchedPixels:
 * 0`, which is GUARANTEED to mismatch on every run, deterministically
 * producing `*-actual-*.png` / `*-diff-*.png` attachments under
 * `.vitest-attachments/tests/component/__negative_control__/`.
 *
 * Gated behind `VITE_VRT_NEGATIVE_CONTROL=true` (set only by the CI
 * negative-control step) so it never runs — and never fails — as part of
 * the normal `pnpm test:vrt:component` invocation used by AC6 / local dev.
 * This test file executes inside the Vitest Browser Mode iframe context
 * (not Node), so the env var must go through Vite's `import.meta.env`
 * (`VITE_`-prefixed, per Vite's default `envPrefix`) rather than
 * `process.env`, which is undefined in that context.
 */
const RUN_NEGATIVE_CONTROL = import.meta.env.VITE_VRT_NEGATIVE_CONTROL === 'true'

test.skipIf(!RUN_NEGATIVE_CONTROL)(
  'GIVEN a deliberately mismatched fixture WHEN captured THEN toMatchScreenshot() fails and writes actual/diff attachments under the hidden .vitest-attachments directory',
  async () => {
    const el = document.createElement('div')
    el.style.width = '2px'
    el.style.height = '2px'
    el.style.backgroundColor = 'rgb(0, 255, 0)'
    document.body.appendChild(el)
    try {
      await expect(page.elementLocator(el)).toMatchScreenshot(
        'hidden-attachment-negative-control.png',
        {
          comparatorOptions: {
            allowedMismatchedPixels: 0,
          },
        },
      )
    } finally {
      el.remove()
    }
  },
)
