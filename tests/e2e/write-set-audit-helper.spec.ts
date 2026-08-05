/**
 * Focused E2E test for `./write-set-audit-helper`'s `securityOrigin` /
 * `storageKey` metadata retention (Issue #1993, follow-up to PR #1989
 * review, issuecomment-5179331591 / issuecomment-5185109001).
 *
 * This spec exercises `attachWriteSetAudit()` directly against a blank page
 * on the standard preview server (no app code / production key
 * dependencies), and asserts that every recorded `WriteOp` — for each of the
 * four underlying CDP `DOMStorage.*` event kinds this module listens to
 * (`domStorageItemAdded` / `domStorageItemUpdated` / `domStorageItemRemoved`
 * / `domStorageItemsCleared`) — carries a non-empty `securityOrigin` that
 * matches this page's own origin, and a non-empty `storageKey` string (the
 * exact `storageKey` serialization format is a CDP/Chromium implementation
 * detail this spec deliberately does not pin down — see module-level doc
 * comment in `write-set-audit-helper.ts`).
 *
 * This spec does NOT assert any boundary enforcement (filtering, rejection,
 * multi-origin/OOPIF isolation) — Issue #1993 is metadata retention only,
 * per its Out of Scope section.
 */

import { test, expect } from '@playwright/test'
import { attachWriteSetAudit } from './write-set-audit-helper'

test(
  'write-set-audit-helper: GIVEN a page with the CDP write-set audit attached ' +
    'WHEN setItem/update/removeItem/clear localStorage mutations occur ' +
    'THEN every recorded WriteOp carries this page\'s securityOrigin and a non-empty storageKey',
  async ({ page, context, baseURL }) => {
    test.setTimeout(30_000)

    // Attached after the page exists but before the first navigation — same
    // precondition every other caller of this helper must satisfy (see
    // write-set-audit-helper.ts module-level doc comment).
    const { writeLog, flushAudit } = await attachWriteSetAudit(context, page)

    await page.goto('./')

    const expectedOrigin = new URL(baseURL ?? 'http://127.0.0.1:4173').origin
    expect(page.url().startsWith(expectedOrigin)).toBe(true)

    await page.evaluate(() => {
      window.localStorage.setItem('audit-metadata-probe', 'initial')
      window.localStorage.setItem('audit-metadata-probe', 'updated')
      window.localStorage.removeItem('audit-metadata-probe')
      window.localStorage.setItem('audit-metadata-probe-2', 'x')
      window.localStorage.clear()
    })

    // Causal drain barrier — required before reading writeLog for
    // assertions (see write-set-audit-helper.ts flushAudit() doc comment).
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
      // behavior) they are populated with real, non-empty values sourced
      // from the underlying CDP DOMStorage.StorageId for this page's
      // single origin/target — not left blank or defaulted.
      expect(op.securityOrigin).toBe(expectedOrigin)
      expect(typeof op.storageKey).toBe('string')
      expect(op.storageKey.length).toBeGreaterThan(0)
    }

    // All ops originate from the same page/target in this single-origin
    // scenario, so storageKey must be identical across every op (metadata
    // retention only — this spec does not assert cross-origin isolation,
    // which Issue #1993 explicitly leaves out of scope).
    const distinctStorageKeys = new Set(probeOps.map((op) => op.storageKey))
    expect(distinctStorageKeys.size).toBe(1)
  },
)
