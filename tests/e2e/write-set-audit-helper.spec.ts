/**
 * Focused E2E test for `./write-set-audit-helper`'s `securityOrigin` /
 * `storageKey` metadata retention (Issue #1993, follow-up to PR #1989
 * review, issuecomment-5179331591 / issuecomment-5185109001).
 *
 * PR #1996 OWNER REQUEST_CHANGES repair (issuecomment-5191160187, P1
 * blocker 1): the previous version of this spec only asserted
 * `securityOrigin === expectedOrigin`, `storageKey.length > 0`, and that
 * every recorded `storageKey` was mutually identical. That set of
 * assertions cannot distinguish a real `storageKey` from a field-swap bug
 * (e.g. `record(type, key, securityOrigin, securityOrigin)` instead of
 * `record(type, key, securityOrigin, storageKey)`) — a swapped-in
 * `securityOrigin` value is itself non-empty and identical across every
 * op, so it would pass all three assertions. This spec now obtains the
 * REAL storage key from an independent oracle — a second, separate CDP
 * session (`context.newCDPSession(page)` -> `Storage.getStorageKey`, a
 * different CDP domain from the `DOMStorage` domain
 * `attachWriteSetAudit()` itself uses) — and asserts exact equality
 * against that independently-obtained value, not just non-emptiness/
 * mutual consistency. The verifier session is detached once its one-shot
 * oracle read is done.
 *
 * PR #1996 OWNER REQUEST_CHANGES repair (issuecomment-5191160187, P2): this
 * spec previously navigated to `page.goto('./')`, which under
 * `playwright.config.ts` resolves to the Playwright preview server serving
 * the actual built product app (Vite `preview`), not a blank/inert
 * fixture — despite the previous version's comments describing it as
 * blank. That coupled this helper-focused test to the app's own storage
 * lifecycle (e.g. any future app-level `localStorage.clear()` during init
 * would have polluted `probeOps`). This spec now intercepts navigation via
 * `page.route()` and serves a minimal inert same-origin HTML fixture via
 * `route.fulfill()`, so this test is independent of the app bundle,
 * production storage keys, and any app-level storage lifecycle behavior.
 * The synthetic fixture path stays on the SAME origin as `baseURL`
 * (`page.route` intercepts same-origin requests before they hit the
 * network), so the `securityOrigin`/oracle assertions below remain valid.
 *
 * This spec does NOT assert any boundary enforcement (filtering, rejection,
 * multi-origin/OOPIF isolation) — Issue #1993 is metadata retention only,
 * per its Out of Scope section.
 */

import { test, expect } from '@playwright/test'
import type { CDPSession } from '@playwright/test'
import { attachWriteSetAudit } from './write-set-audit-helper'

const FIXTURE_PATH = '/__write-set-audit-fixture__'
const FIXTURE_HTML = '<!doctype html><html><head><title>write-set audit fixture</title></head><body></body></html>'

async function send<T>(client: CDPSession, method: string, params?: object): Promise<T> {
  // Cast via `unknown` because `@playwright/test`'s `CDPSession.send`
  // typings only cover a fixed enumerated set of CDP methods/domains and do
  // not include the `Storage`/`Page` domain methods used below.
  return (client.send as unknown as (m: string, p?: object) => Promise<T>)(method, params)
}

async function readRealStorageKey(client: CDPSession): Promise<string> {
  // `Storage.getStorageKey` (a DIFFERENT CDP domain from `DOMStorage`, which
  // `attachWriteSetAudit()` itself listens on) is this spec's independent
  // oracle for detecting securityOrigin/storageKey field-swap bugs (PR
  // #1996 review, issuecomment-5191160187, P1 blocker 1). Empirically
  // verified (scratch probe, PR #1996 repair iteration 1): calling it
  // WITHOUT a `frameId` fails with "Target is not a supported worker type
  // for storage inspection" against a Page-bound CDPSession, so this
  // resolves the main frame's id via `Page.getFrameTree` first. Also
  // empirically verified: `storageKey` and `securityOrigin` are NOT
  // byte-identical strings for the same page/origin (Chromium's storage
  // key serialization appends a trailing `/`, e.g. `securityOrigin:
  // "http://example.com"` vs. `storageKey: "http://example.com/"`), so an
  // exact-match assertion against this oracle's `storageKey` value is a
  // genuine swap-detector, not a coincidental match.
  await send(client, 'Page.enable')
  const frameTree = await send<{ frameTree: { frame: { id: string } } }>(client, 'Page.getFrameTree')
  const frameId = frameTree.frameTree.frame.id
  const result = await send<{ storageKey: string }>(client, 'Storage.getStorageKey', { frameId })
  return result.storageKey
}

test(
  'write-set-audit-helper: GIVEN a page with the CDP write-set audit attached ' +
    'WHEN setItem/update/removeItem/clear localStorage mutations occur ' +
    'THEN every recorded WriteOp carries this page\'s securityOrigin and the real CDP storageKey',
  async ({ page, context, baseURL }) => {
    test.setTimeout(30_000)

    // PR #1996 review P2 repair: serve a minimal same-origin inert fixture
    // instead of navigating into the actual built product app, so this
    // helper-focused test is independent of the app's own storage
    // lifecycle.
    await page.route(`**${FIXTURE_PATH}`, (route) =>
      route.fulfill({ contentType: 'text/html', body: FIXTURE_HTML }),
    )

    // Attached after the page exists but before the first navigation — same
    // precondition every other caller of this helper must satisfy (see
    // write-set-audit-helper.ts module-level doc comment).
    const { writeLog, flushAudit } = await attachWriteSetAudit(context, page)

    await page.goto(FIXTURE_PATH)

    const expectedOrigin = new URL(baseURL ?? 'http://127.0.0.1:4173').origin
    expect(page.url().startsWith(expectedOrigin)).toBe(true)

    // PR #1996 review P1 blocker 1 repair: independent oracle for the real
    // CDP storage key, obtained from a SEPARATE CDP session so this
    // assertion cannot be satisfied by a field-swap bug that merely echoes
    // `securityOrigin` into the `storageKey` slot.
    const verifier = await context.newCDPSession(page)
    let expectedStorageKey: string
    try {
      expectedStorageKey = await readRealStorageKey(verifier)
    } finally {
      await verifier.detach()
    }
    expect(expectedStorageKey.length).toBeGreaterThan(0)

    await page.evaluate(() => {
      window.localStorage.setItem('audit-metadata-probe', 'initial')
      window.localStorage.setItem('audit-metadata-probe', 'updated')
      window.localStorage.removeItem('audit-metadata-probe')
      window.localStorage.setItem('audit-metadata-probe-2', 'x')
      window.localStorage.clear()
    })

    // Causal drain barrier — required before reading writeLog for
    // assertions (see write-set-audit-helper.ts flushAudit() doc comment).
    // This also throws if attachWriteSetAudit() recorded a fail-closed
    // auditFailure (PR #1996 review P2 repair) for any observed event.
    await flushAudit()

    const probeOps = writeLog.filter(
      (op) => op.key === 'audit-metadata-probe' || op.key === 'audit-metadata-probe-2' || op.type === 'clear',
    )

    // GIVEN/WHEN above should have produced: setItem (initial), setItem
    // (update -> domStorageItemUpdated), removeItem, setItem (probe-2),
    // clear — one WriteOp per underlying CDP DOMStorage.* event kind this
    // module listens to.
    expect(probeOps.length).toBe(5)

    const setItemOps = probeOps.filter((op) => op.type === 'setItem')
    const removeItemOps = probeOps.filter((op) => op.type === 'removeItem')
    const clearOps = probeOps.filter((op) => op.type === 'clear')
    expect(setItemOps.length).toBe(3)
    expect(removeItemOps.length).toBe(1)
    expect(clearOps.length).toBe(1)

    for (const op of probeOps) {
      // AC1/AC2 (write-set-audit-helper.ts): every WriteOp carries a
      // securityOrigin/storageKey field, and here (Issue #1993 core
      // behavior) they are populated with real values sourced from the
      // underlying CDP DOMStorage.StorageId for this page's single
      // origin/target — not left blank, defaulted, or field-swapped.
      expect(op.securityOrigin).toBe(expectedOrigin)
      // PR #1996 review P1 blocker 1 repair: exact match against the
      // independently-obtained oracle value, not just non-empty/mutually
      // consistent — this is what actually catches a securityOrigin <->
      // storageKey field swap (a swapped-in securityOrigin value would be
      // non-empty and self-consistent, but would NOT equal this
      // independently-obtained real storageKey unless the two happened to
      // be byte-identical strings, which they are not for this app's
      // Chromium storage-key serialization).
      expect(op.storageKey).toBe(expectedStorageKey)
    }

    // All ops originate from the same page/target in this single-origin
    // scenario, so storageKey must be identical across every op (metadata
    // retention only — this spec does not assert cross-origin isolation,
    // which Issue #1993 explicitly leaves out of scope).
    const distinctStorageKeys = new Set(probeOps.map((op) => op.storageKey))
    expect(distinctStorageKeys.size).toBe(1)
  },
)
