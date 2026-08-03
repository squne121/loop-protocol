/**
 * E2E: M4 preview-mode storage namespace + nested base artifact behavior
 * (Issue #1283, AC9 / AC15 / AC16)
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
 * fallback has been removed: `LOOP_EXPECTED_STORAGE_KEY` is a REQUIRED
 * environment variable, and this spec throws immediately (a hard FAIL, not
 * a silent skip) if it is unset OR equals the production key. This spec is
 * also excluded from the default `playwright.config.ts` test run
 * (`testIgnore`) and only included when
 * `LOOP_E2E_PREVIEW_NAMESPACE_LANE=true` selects the dedicated lane
 * (`testMatch`) — see `playwright.config.ts` and the
 * `test:e2e:preview-namespace` package.json script.
 *
 * 2026-08-03 OWNER review repair (issuecomment-5165010968, AC9/AC15/AC16):
 *
 * - Production sentinel is now seeded exactly once via
 *   `browser.newContext({ storageState })` (context-level, before the
 *   first navigation) instead of `page.addInitScript()`, matching the
 *   Design Constraints' "initialized once at context level" requirement.
 * - AC9/AC15 now also verify deploy-pr-equivalent NESTED BASE behavior:
 *   `playwright.config.ts`'s `baseURL` (derived from the same
 *   `VITE_BASE_PATH` env var used to build `dist/`) is asserted to be a
 *   real nested path (not root); the final `page.url()` pathname must be
 *   exactly that nested path; every script/stylesheet request must be
 *   under the nested prefix; every observed asset request must be under
 *   the nested prefix (zero root-relative requests); every response must
 *   be 2xx; and there must be zero `requestfailed` events and zero 404
 *   responses.
 * - A Node-side write-set operation-history audit (same mechanism as
 *   `m4-upgrade-loop.spec.ts`: `context.exposeBinding()` +
 *   `context.addInitScript()` patching `Storage.prototype`) now verifies
 *   that the production key AND any E2E-runtime-shaped key
 *   (`loop-protocol.e2e.*`) receive zero operations, and that only the
 *   resolved preview-namespace key is written (AC15).
 *
 * Dedicated lane invocation (see `pnpm run test:e2e:preview-namespace`):
 *
 *   VITE_BASE_PATH=/loop-protocol/pr-1283/ \
 *   VITE_LOOP_STORAGE_NAMESPACE=pr-1283 \
 *   LOOP_EXPECTED_STORAGE_KEY=loop-protocol.preview.pr-1283.mvp.save \
 *     pnpm run test:e2e:preview-namespace
 *
 * `pnpm run test:e2e:preview-namespace` self-validates namespace / expected
 * key / expected base path (fail-closed, before build), self-fixes
 * `VITE_E2E_MODE=false` (does not rely on caller env), removes any stale
 * `dist/`, builds a production-like bundle (still honoring
 * `VITE_LOOP_STORAGE_NAMESPACE` and `VITE_BASE_PATH` via Vite's
 * `import.meta.env` build-time replacement regardless of `VITE_E2E_MODE`),
 * runs the production artifact boundary checker
 * (`pnpm dist:e2e-boundary --mode production --dist dist`), and only then
 * runs Playwright with `LOOP_E2E_PREVIEW_NAMESPACE_LANE=true`, which also
 * forces `reuseExistingServer: false` in `playwright.config.ts` so a stale
 * server from a different worktree/build cannot be reused.
 */

import { test, expect, type Page, type BrowserContext, type Browser } from '@playwright/test'

const PRODUCTION_KEY = 'loop-protocol.mvp.save'
// Matches the E2E-runtime key shape m4-upgrade-loop.spec.ts's
// buildE2EStorageKey() produces (`loop-protocol.e2e.<worker scope>.mvp.save`).
// This production-like build never sets VITE_E2E_MODE and never installs an
// __LOOP_STORAGE_KEY__ runtime override, so no such key should ever be
// touched — but the write-set audit asserts this from operation history,
// not by absence of evidence.
const E2E_RUNTIME_KEY_PATTERN = /^loop-protocol\.e2e\./

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

type WriteOp = {
  type: 'setItem' | 'removeItem' | 'clear'
  key: string | null
  order: number
}

/**
 * Context-level, one-time production sentinel seeding (Design Constraints:
 * "production sentinel と E2E seed は最初の navigation 前に一度だけ
 * context-level で初期化") + the same write-set operation-history audit
 * used by m4-upgrade-loop.spec.ts. This lane never needs a reload, but the
 * one-time-init requirement and audit mechanism are shared with the
 * runtime lane deliberately (single source of truth for the pattern).
 */
async function createAuditedContext(
  browser: Browser,
  baseURL: string,
): Promise<{ context: BrowserContext; page: Page; writeLog: WriteOp[] }> {
  const writeLog: WriteOp[] = []

  const context = await browser.newContext({
    baseURL,
    storageState: {
      cookies: [],
      origins: [
        {
          origin: new URL(baseURL).origin,
          localStorage: [{ name: PRODUCTION_KEY, value: PRODUCTION_SENTINEL }],
        },
      ],
    },
  })

  await context.exposeBinding(
    '__loopE2EWriteLog',
    (_source: unknown, op: { type: 'setItem' | 'removeItem' | 'clear'; key: string | null; order: number }) => {
      writeLog.push(op)
    },
  )

  await context.addInitScript(() => {
    let order = 0
    const w = window as Window & { __loopE2EWriteLog?: (op: unknown) => void }
    const originalSetItem = Storage.prototype.setItem
    const originalRemoveItem = Storage.prototype.removeItem
    const originalClear = Storage.prototype.clear

    Storage.prototype.setItem = function patchedSetItem(key: string, value: string) {
      if (this === window.localStorage && w.__loopE2EWriteLog) {
        w.__loopE2EWriteLog({ type: 'setItem', key, order: order++ })
      }
      return originalSetItem.call(this, key, value)
    }
    Storage.prototype.removeItem = function patchedRemoveItem(key: string) {
      if (this === window.localStorage && w.__loopE2EWriteLog) {
        w.__loopE2EWriteLog({ type: 'removeItem', key, order: order++ })
      }
      return originalRemoveItem.call(this, key)
    }
    Storage.prototype.clear = function patchedClear() {
      if (this === window.localStorage && w.__loopE2EWriteLog) {
        w.__loopE2EWriteLog({ type: 'clear', key: null, order: order++ })
      }
      return originalClear.call(this)
    }
  })

  const page = await context.newPage()
  return { context, page, writeLog }
}

test(
  'M4 upgrade loop: AC9/AC15 GIVEN a production-like nested-base build with VITE_LOOP_STORAGE_NAMESPACE WHEN New Game -> Save THEN only the resolved preview-namespace key is written, the production sentinel and any E2E-runtime key remain untouched, and every navigation/asset request stays under the nested VITE_BASE_PATH prefix',
  async ({ browser }, testInfo) => {
    test.setTimeout(30_000)

    const baseURL = testInfo.project.use.baseURL as string | undefined
    expect(baseURL, 'playwright.config.ts must resolve a baseURL for this lane').toBeTruthy()
    const nestedBasePath = new URL(baseURL!).pathname
    expect(
      nestedBasePath,
      'VITE_BASE_PATH must resolve to a real nested path, not root — the dedicated ' +
        'preview-namespace lane invocation always sets VITE_BASE_PATH ' +
        '(e.g. /loop-protocol/pr-1283/); a root path would make the nested-base assertions ' +
        'below vacuous (AC9).',
    ).not.toBe('/')

    const { context, page, writeLog } = await createAuditedContext(browser, baseURL!)

    const allRequests: Array<{ url: string; resourceType: string }> = []
    const failedRequests: string[] = []
    const responseStatuses: Array<{ url: string; status: number }> = []

    page.on('request', (request) => {
      allRequests.push({ url: request.url(), resourceType: request.resourceType() })
    })
    page.on('requestfailed', (request) => {
      failedRequests.push(request.url())
    })
    page.on('response', (response) => {
      responseStatuses.push({ url: response.url(), status: response.status() })
    })

    try {
      // Deliberately does NOT set __LOOP_STORAGE_KEY__ — this lane relies on
      // the build-time VITE_LOOP_STORAGE_NAMESPACE resolution only. This
      // build is production-like (no VITE_E2E_MODE), so
      // __LOOP_E2E_BOOTSTRAP__ / __LOOP_E2E__ are not present in the bundle
      // and are not referenced here.
      // './' (not '/') — a leading '/' is an absolute path that REPLACES the
      // baseURL's path segment entirely (new URL('/', base).pathname === '/'),
      // which would silently navigate to root and make the nested-base
      // assertions below vacuous when baseURL has a non-root VITE_BASE_PATH
      // (Issue #1283 AC9 repair).
      await page.goto('./')

      // AC9: final navigated pathname must be the exact nested path.
      const finalPathname = new URL(page.url()).pathname
      expect(
        finalPathname,
        `final page.url() pathname must be the exact nested path (${nestedBasePath}) (AC9)`,
      ).toBe(nestedBasePath)

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
        'only the production sentinel and the resolved preview-namespace key must exist ' +
          '(AC9/AC15 sanity check)',
      ).toEqual([PRODUCTION_KEY, EXPECTED_KEY].sort())

      // AC15: the write-set operation-history audit must record zero
      // operations against the production key or any E2E-runtime-shaped key,
      // and zero clear() calls (which implicitly operate outside scope).
      const clearOps = writeLog.filter((op) => op.type === 'clear')
      expect(
        clearOps,
        'write-set operation history must record zero clear() calls (AC15)',
      ).toEqual([])

      const productionKeyOps = writeLog.filter((op) => op.key === PRODUCTION_KEY)
      expect(
        productionKeyOps,
        'write-set operation history must record zero operations against the production key (AC15)',
      ).toEqual([])

      const e2eRuntimeKeyOps = writeLog.filter(
        (op) => op.key !== null && E2E_RUNTIME_KEY_PATTERN.test(op.key),
      )
      expect(
        e2eRuntimeKeyOps,
        'write-set operation history must record zero operations against any E2E-runtime-shaped key (AC15)',
      ).toEqual([])

      const outOfScopeOps = writeLog.filter((op) => op.type !== 'clear' && op.key !== EXPECTED_KEY)
      expect(
        outOfScopeOps,
        'write-set operation history must record zero operations outside the resolved preview ' +
          'namespace key (AC15)',
      ).toEqual([])

      const inScopeOps = writeLog.filter((op) => op.type !== 'clear' && op.key === EXPECTED_KEY)
      expect(
        inScopeOps.length,
        'write-set operation history must have captured at least the Save write (AC15 harness sanity check)',
      ).toBeGreaterThan(0)

      // AC9: every script/stylesheet request must be under the nested prefix.
      const scriptAndStyleRequests = allRequests.filter((r) =>
        r.resourceType === 'script' || r.resourceType === 'stylesheet',
      )
      expect(
        scriptAndStyleRequests.length,
        'at least one script or stylesheet request must have been observed (AC9 harness sanity check)',
      ).toBeGreaterThan(0)
      const rootRelativeScriptOrStyle = scriptAndStyleRequests.filter(
        (r) => !new URL(r.url).pathname.startsWith(nestedBasePath),
      )
      expect(
        rootRelativeScriptOrStyle,
        'every JS/CSS asset request must be under the nested VITE_BASE_PATH prefix (AC9)',
      ).toEqual([])

      // AC9: zero root-relative (non-nested) asset requests of any kind
      // (document, script, stylesheet, image, font, fetch, etc.) against the
      // same origin as the preview server.
      const sameOriginRequests = allRequests.filter(
        (r) => new URL(r.url).origin === new URL(baseURL!).origin,
      )
      const rootRelativeRequests = sameOriginRequests.filter(
        (r) => !new URL(r.url).pathname.startsWith(nestedBasePath),
      )
      expect(
        rootRelativeRequests,
        'zero root-relative (non-nested) asset requests must be observed (AC9)',
      ).toEqual([])

      // AC9: zero requestfailed events and zero 404 responses; all responses 2xx.
      expect(failedRequests, 'zero requestfailed events must be observed (AC9)').toEqual([])
      const notFoundResponses = responseStatuses.filter((r) => r.status === 404)
      expect(notFoundResponses, 'zero 404 responses must be observed (AC9)').toEqual([])
      const nonSuccessResponses = responseStatuses.filter((r) => r.status < 200 || r.status >= 300)
      expect(
        nonSuccessResponses,
        'every asset response must be 2xx (AC9)',
      ).toEqual([])
    } finally {
      await context.close()
    }
  },
)
