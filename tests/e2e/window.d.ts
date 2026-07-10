/**
 * Global `Window` type augmentation for the VRT visual scenario fixture
 * (Issue #1385). Lets e2e spec files reference
 * `window.__LOOP_VISUAL_SCENARIO__` (e.g. via `page.evaluate()`) without a
 * local cast. Mirrors the inline type declared in `src/main.ts` — kept in
 * sync manually since `src/main.ts` never imports this test-only module.
 */

import type { VisualScenarioFixture } from './visual-utils'

declare global {
  interface Window {
    __LOOP_VISUAL_SCENARIO__?: VisualScenarioFixture
  }
}

export {}
