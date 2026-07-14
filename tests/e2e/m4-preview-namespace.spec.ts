/**
 * E2E: M4 preview-mode storage namespace (Issue #1283, AC9)
 *
 * Dedicated lane: unlike `m4-upgrade-loop.spec.ts` (which uses the runtime
 * `__LOOP_STORAGE_KEY__` override), this scenario relies on the build-time
 * `VITE_LOOP_STORAGE_NAMESPACE` resolution baked into a preview-mode build
 * (`resolveStorageKey()` only falls back to the preview namespace when no
 * runtime override is present — Design Constraints: "E2E runtime namespace
 * and preview build namespace are verified in separate scenarios").
 *
 * Intended invocation (see Issue #1283 Verification Commands):
 *
 *   VITE_E2E_MODE=true VITE_LOOP_STORAGE_NAMESPACE=pr-1283 pnpm build
 *   LOOP_EXPECTED_STORAGE_KEY=loop-protocol.preview.pr-1283.mvp.save \
 *     pnpm exec playwright test tests/e2e/m4-preview-namespace.spec.ts
 *
 * `LOOP_EXPECTED_STORAGE_KEY` is a Node-side (test-runner) environment
 * variable naming the key this run's build is expected to resolve to. When
 * unset (e.g. a plain `pnpm test:e2e` full run with no namespace build), it
 * defaults to the production key — i.e. the assertions degrade to "the
 * default (production) key round-trips a save", which remains a
 * non-vacuous check of the same save/read path without asserting anything
 * about namespace isolation in that lane.
 */

import { test, expect, type Page } from '@playwright/test'

const PRODUCTION_KEY = 'loop-protocol.mvp.save'
const EXPECTED_KEY = process.env.LOOP_EXPECTED_STORAGE_KEY ?? PRODUCTION_KEY

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
  'M4 upgrade loop: AC9 GIVEN preview-mode build (or default build) WHEN New Game -> Save THEN only the resolved namespace key is written and the production sentinel is untouched',
  async ({ page }) => {
    test.setTimeout(30_000)

    // Deliberately does NOT set __LOOP_STORAGE_KEY__ — this lane relies on the
    // build-time VITE_LOOP_STORAGE_NAMESPACE resolution (or its absence).
    await page.addInitScript(
      (payload: { productionKey: string; productionSentinel: string }) => {
        window.localStorage.setItem(payload.productionKey, payload.productionSentinel)
        ;(
          window as Window & { __LOOP_E2E_BOOTSTRAP__?: { autoStart?: boolean } }
        ).__LOOP_E2E_BOOTSTRAP__ = { autoStart: false }
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

    if (EXPECTED_KEY !== PRODUCTION_KEY) {
      // Dedicated preview-namespace lane: production sentinel must be untouched,
      // and the app must not have used the runtime override key (AC9, AC7).
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
    }
  },
)
