/**
 * Shared CDP-based write-set operation-history audit core (Issue #1283
 * repair iteration 1; extracted into this shared module by Issue #1987).
 *
 * Both `m4-upgrade-loop.spec.ts` and `m4-preview-namespace.spec.ts` used to
 * duplicate an identical CDP `DOMStorage`-domain audit implementation
 * (extracting it was previously outside Issue #1283's Allowed Paths). This
 * module extracts ONLY the CDP audit core — not lane-specific
 * `browser.newContext()` / `storageState` seeding / `addInitScript()`
 * runtime overrides, which stay inline in each spec file because the two
 * lanes deliberately differ (the runtime lane seeds a per-test E2E key and
 * installs a runtime `__LOOP_STORAGE_KEY__` override via `addInitScript`;
 * the preview lane relies purely on the build-time
 * `VITE_LOOP_STORAGE_NAMESPACE` resolution and must never receive a runtime
 * storage override, or the build-time namespace assertions would be
 * silently defeated).
 *
 * Assumptions / preconditions (both callers already satisfy these; do not
 * reuse this helper outside those constraints without re-verifying them):
 *
 * - Chromium only. Implemented via the CDP `DOMStorage` domain
 *   (`DOMStorage.domStorageItemAdded/Updated/Removed/ItemsCleared`), which
 *   is Chromium-specific. `playwright.config.ts` only configures a
 *   `chromium` project in this repo, so this is not a gap in practice.
 * - Single page, single origin per `BrowserContext`. `attachWriteSetAudit`
 *   opens exactly one CDP session bound to the given `page`/target and
 *   assumes all `localStorage` mutations of interest happen on that page's
 *   single origin. It does not track additional pages/tabs/frames opened
 *   later in the same context.
 * - Must be attached AFTER the `page` is created but BEFORE the first
 *   `page.goto()` navigation, and after any `context.newPage()` /
 *   `storageState` seeding has already applied — empirically verified
 *   (Issue #1283 repair iteration 1 scratch probe) that `storageState`
 *   seeding applied by Playwright at context-creation time does NOT emit
 *   any `DOMStorage.*` events once this audit's CDP session enables the
 *   `DOMStorage` domain, so attaching before the first navigation never
 *   spuriously records the initial seed writes as app-triggered
 *   operations.
 * - The returned CDP session survives `page.reload()` (bound to the
 *   page/target, not the document), so `order`/`documentEpoch`/`epochOrder`
 *   tracking remains valid and continuous across reloads within the same
 *   page.
 *
 * `flushAudit()` contract: performs a round-trip `send()` on the SAME CDP
 * session used for the audit. CDP delivers messages on a single session
 * strictly in the order the browser sent them, so any `DOMStorage.*` event
 * already dispatched by the browser before `flushAudit()` is called is
 * guaranteed to have been delivered to this module's event handlers (and
 * therefore be present in `writeLog`) before `flushAudit()`'s returned
 * promise resolves. Callers MUST await `flushAudit()` immediately before
 * `page.reload()` and immediately before reading `writeLog` for assertions
 * — without this drain barrier, a fire-and-forget event delivery race could
 * silently produce a false-green (an in-scope or out-of-scope mutation that
 * had already fired at the browser level but had not yet reached
 * `writeLog`).
 */

import type { BrowserContext, CDPSession, Page } from '@playwright/test'

export type WriteOp = {
  type: 'setItem' | 'removeItem' | 'clear'
  key: string | null
  /** Global, monotonically-increasing sequence number assigned from this
   *  Node-side counter. Never reset by navigation/reload (P1 Blocker 1). */
  order: number
  /** Bumped on every main-frame `framenavigated` (including reload) — proves
   *  operations are attributable to a specific navigation/document, and lets
   *  callers prove the audit actually observed operations spanning a reload
   *  boundary, not just before it (P1 Blocker 1). */
  documentEpoch: number
  /** Sequence local to `documentEpoch`, reset to 0 on every navigation. The
   *  CDP `DOMStorage` domain does not expose a frame/document id, so this
   *  (navigation-epoch, local-sequence) pair is the practical equivalent of
   *  a per-document sequence for a single-frame same-origin scenario. */
  epochOrder: number
}

/**
 * Attaches the CDP `DOMStorage`-domain write-set operation-history audit to
 * an already-created `context`/`page` pair. Installs a Node-side monotonic
 * `order` counter (never reset by navigation/reload — P1 Blocker 1) and
 * observes every localStorage mutation at the browser-engine level via CDP,
 * independent of which JS API triggered it (P1 Blocker 3: this also catches
 * named-property `localStorage[key] = value` / `delete localStorage[key]`
 * access, which a `Storage.prototype` monkey-patch cannot intercept).
 *
 * Callers are responsible for creating `context`/`page` (including any
 * lane-specific `storageState` seeding and `addInitScript` runtime
 * overrides) and for calling this function after `page` exists but before
 * the first navigation — see the module-level doc comment above for the
 * full precondition set.
 */
export async function attachWriteSetAudit(
  context: BrowserContext,
  page: Page,
): Promise<{ writeLog: WriteOp[]; flushAudit: () => Promise<void> }> {
  const writeLog: WriteOp[] = []

  let globalOrder = 0
  let documentEpoch = 0
  let epochOrder = 0

  page.on('framenavigated', (frame) => {
    if (frame === page.mainFrame()) {
      documentEpoch += 1
      epochOrder = 0
    }
  })

  const client: CDPSession = await context.newCDPSession(page)
  await client.send('DOMStorage.enable')

  function record(type: WriteOp['type'], key: string | null): void {
    writeLog.push({ type, key, order: globalOrder, documentEpoch, epochOrder })
    globalOrder += 1
    epochOrder += 1
  }

  client.on('DOMStorage.domStorageItemAdded', (event) => {
    if (event.storageId.isLocalStorage) record('setItem', event.key)
  })
  client.on('DOMStorage.domStorageItemUpdated', (event) => {
    if (event.storageId.isLocalStorage) record('setItem', event.key)
  })
  client.on('DOMStorage.domStorageItemRemoved', (event) => {
    if (event.storageId.isLocalStorage) record('removeItem', event.key)
  })
  client.on('DOMStorage.domStorageItemsCleared', (event) => {
    if (event.storageId.isLocalStorage) record('clear', null)
  })

  // P1 Blocker 2 fix: forces delivery of any DOMStorage event the browser
  // already sent on this SAME CDP session before this call resolves (CDP
  // messages on one session are delivered strictly in send order), so
  // callers can await this immediately before reload / before reading
  // writeLog for assertions.
  async function flushAudit(): Promise<void> {
    await client.send('DOMStorage.enable')
  }

  return { writeLog, flushAudit }
}
