/**
 * E2E: M4 preview-mode storage namespace (Issue #1283, AC9)
 *
 * Dedicated lane ONLY: unlike `m4-upgrade-loop.spec.ts` (which uses the
 * runtime `__LOOP_STORAGE_KEY__` override), this scenario relies on the
 * build-time `VITE_LOOP_STORAGE_NAMESPACE` resolution baked into a
 * production-like build (`resolveStorageKey()` only falls back to the
 * preview namespace when no runtime override is present — Design
 * Constraints: "E2E runtime namespace and preview build namespace are
 * verified in separate scenarios").
 *
 * PR #1517 review fix (P0 Blocker 1): this spec previously fell back to
 * asserting against the *production* key when `LOOP_EXPECTED_STORAGE_KEY`
 * was unset, which let the standard `VITE_E2E_MODE=true`-only CI E2E job
 * "pass" this test without ever exercising real namespace isolation. That
 * fallback has been removed: `LOOP_EXPECTED_STORAGE_KEY` is now a REQUIRED
 * environment variable, and this spec throws immediately (a hard FAIL, not
 * a silent skip) if it is unset OR equals the production key. This spec is
 * also excluded from the default `playwright.config.ts` test run
 * (`testIgnore`) and only included when
 * `LOOP_E2E_PREVIEW_NAMESPACE_LANE=true` selects the dedicated lane
 * (`testMatch`) — see `playwright.config.ts` and the
 * `test:e2e:preview-namespace` package.json script.
 *
 * Dedicated lane invocation (see `pnpm run test:e2e:preview-namespace`):
 *
 *   VITE_LOOP_STORAGE_NAMESPACE=pr-1283 \
 *   LOOP_EXPECTED_STORAGE_KEY=loop-protocol.preview.pr-1283.mvp.save \
 *     pnpm run test:e2e:preview-namespace
 *
 * `pnpm run test:e2e:preview-namespace` removes any stale `dist/`, builds
 * WITHOUT `VITE_E2E_MODE` (a production-like build — `VITE_LOOP_STORAGE_NAMESPACE`
 * is still honored by Vite's `import.meta.env` replacement regardless of
 * `VITE_E2E_MODE`), and runs Playwright with `LOOP_E2E_PREVIEW_NAMESPACE_LANE=true`,
 * which also forces `reuseExistingServer: false` in `playwright.config.ts` so
 * a stale server from a different worktree/build cannot be reused.
 */

import { test, expect, type Page } from '@playwright/test'

const PRODUCTION_KEY = 'loop-protocol.mvp.save'

const rawExpectedKey = process.env.LOOP_EXPECTED_STORAGE_KEY
if (!rawExpectedKey || rawExpectedKey.trim() === '') {
  throw new Error(
    'LOOP_EXPECTED_STORAGE_KEY is required for tests/e2e/m4-preview-namespace.spec.ts ' +
      '(dedicated preview-namespace lane only — see file header). Refusing to fall back ' +
      'to the production key, which would silently mask a missing namespace build (AC9, ' +
      'PR #1517 review fix).',
  )
}
if (rawExpectedKey === PRODUCTION_KEY) {
  throw new Error(
    `LOOP_EXPECTED_STORAGE_KEY must not equal the production key (${PRODUCTION_KEY}) — ` +
      'this spec exists to prove namespace ISOLATION from the production key, so asserting ' +
      'against the production key itself would be a vacuous check (AC9, PR #1517 review fix).',
  )
}
const EXPECTED_KEY: string = rawExpectedKey

const PRODUCTION_SENTINEL = JSON.stringify({
  schemaVersion: 1,
  resources: 777,
  weaponPower: 3,
  playerMaxHp: 11,
})

type StoredSnapshot = {
  schemaVersion: number
  resources: number
  weaponPower: number
  playerMaxHp: number
}

async function readStorageKey(page: Page, key: string): Promise<StoredSnapshot | null> {
  const raw = await page.evaluate((k: string) => window.localStorage.getItem(k), key)
  if (raw === null) return null
  return JSON.parse(raw) as StoredSnapshot
}

test(
  'M4 upgrade loop: AC9 GIVEN a production-like build with VITE_LOOP_STORAGE_NAMESPACE WHEN New Game -> Save THEN only the resolved preview-namespace key is written and the production sentinel is untouched',
  async ({ page }) => {
    test.setTimeout(30_000)

    // Deliberately does NOT set __LOOP_STORAGE_KEY__ — this lane relies on the
    // build-time VITE_LOOP_STORAGE_NAMESPACE resolution only. This build is
    // production-like (no VITE_E2E_MODE), so __LOOP_E2E_BOOTSTRAP__ /
    // __LOOP_E2E__ are not present in the bundle and are not referenced here.
    await page.addInitScript(
      (payload: { productionKey: string; productionSentinel: string }) => {
        window.localStorage.setItem(payload.productionKey, payload.productionSentinel)
      },
      { productionKey: PRODUCTION_KEY, productionSentinel: PRODUCTION_SENTINEL },
    )
    await page.goto('/')

    // title_menu, no loadable snapshot yet under a fresh namespace key -> New Game.
    const newGameButton = page.locator('[data-action="new-game"]')
    await expect(newGameButton).toBeEnabled({ timeout: 5_000 })
    await newGameButton.click()
    await expect(page.locator('[data-field="status"]')).toHaveText('New Game started.', {
      timeout: 5_000,
    })

    // Save persists the current (fresh) progression snapshot to whatever key
    // resolveStorageKey() resolves to for this build.
    const saveButton = page.locator('[data-action="save"]')
    await expect(saveButton).toBeEnabled({ timeout: 5_000 })
    await saveButton.click()
    await expect(page.locator('[data-field="status"]')).toHaveText('Save complete.', {
      timeout: 5_000,
    })

    // AC9: the resolved namespace key holds a valid snapshot.
    const savedSnapshot = await readStorageKey(page, EXPECTED_KEY)
    expect(
      savedSnapshot,
      `resolved namespace key (${EXPECTED_KEY}) must hold a saved snapshot (AC9)`,
    ).not.toBeNull()
    expect(savedSnapshot!.schemaVersion, 'saved snapshot must be schemaVersion 1 (AC9)').toBe(1)

    // Production sentinel must be untouched (AC7, AC9): namespace isolation
    // means the app never wrote to the production key in this build.
    const productionValue = await page.evaluate(
      (key: string) => window.localStorage.getItem(key),
      PRODUCTION_KEY,
    )
    expect(
      productionValue,
      'production key must remain byte-for-byte unchanged in the preview-namespace lane (AC9, AC7)',
    ).toBe(PRODUCTION_SENTINEL)

    const allKeys = await page.evaluate(() => {
      const keys: string[] = []
      for (let i = 0; i < window.localStorage.length; i += 1) {
        const k = window.localStorage.key(i)
        if (k !== null) keys.push(k)
      }
      return keys.sort()
    })
    expect(
      allKeys,
      'only the production sentinel and the resolved preview-namespace key must exist (AC9)',
    ).toEqual([PRODUCTION_KEY, EXPECTED_KEY].sort())
  },
)
