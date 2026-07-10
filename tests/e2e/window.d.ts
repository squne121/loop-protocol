/**
 * Global `Window` type augmentation for the VRT visual scenario fixture
 * (Issue #1385). Lets e2e spec files reference
 * `window.__LOOP_VISUAL_SCENARIO__` (e.g. via `page.evaluate()`) without a
 * local cast. `VisualScenarioFixture` is re-exported by `./visual-utils`,
 * which itself type-imports it FROM `src/main.ts` (single source of truth,
 * Issue #1385 review additional指摘9) — `src/main.ts` never imports this
 * test-only module back, so the production bundle never depends on it.
 */

import type { VisualScenarioFixture } from './visual-utils'

declare global {
  interface Window {
    __LOOP_VISUAL_SCENARIO__?: VisualScenarioFixture
  }
}

export {}
