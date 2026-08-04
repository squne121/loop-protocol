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
 * `flushAudit()` contract (OWNER REQUEST_CHANGES repair, PR #1989 review,
 * issuecomment-5179331591, P1 blocker): a bare CDP command round-trip (e.g.
 * re-sending `DOMStorage.enable`) is NOT a causal drain. Chromium's
 * `localStorage.setItem()` updates the renderer-side cache synchronously,
 * but notifies the browser process (the source of `DOMStorage.*` CDP
 * events) via an async Mojo `Put()` call — the Inspector-facing event is
 * only generated later, from `StorageAreaObserver::KeyChanged()`. A command
 * round-trip only proves that events already IN FLIGHT to this CDP session
 * have been delivered; it does NOT prove that a JS-level write which just
 * returned has already been turned into a delivered event.
 *
 * `flushAudit()` instead writes-then-deletes a reserved marker key (a
 * per-`attachWriteSetAudit()`-call random key, never exposed in `writeLog`)
 * from the SAME page/origin via `page.evaluate()`, and waits for this
 * module's own listener to observe the marker's
 * `DOMStorage.domStorageItemRemoved` event. Chromium tracks pending storage
 * mutations from a given source in a per-source FIFO queue, so the
 * marker's removal event cannot be observed before every mutation this
 * module's caller triggered earlier from the same page/origin has already
 * been turned into its own event — making the marker's arrival a genuine
 * causal boundary for "every prior mutation from this page has an event
 * recorded in `writeLog`" (not merely "every event already in flight to
 * this session has been delivered"). The marker never remains in
 * `localStorage` after `flushAudit()` resolves (write and delete happen in
 * the same `page.evaluate()` call) and is filtered out of the public
 * `writeLog` so it never pollutes caller assertions (exact key-set checks
 * included). Callers MUST await `flushAudit()` immediately before
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

  // OWNER REQUEST_CHANGES repair (PR #1989 review, issuecomment-5179331591,
  // P1 blocker): reserved marker key used by `flushAudit()` below as a
  // causal drain boundary. Random per `attachWriteSetAudit()` call so it
  // cannot collide with any real application key. Never recorded into the
  // public `writeLog` (filtered in every listener below) and never left
  // behind in `localStorage` (write + delete happen in the same
  // `page.evaluate()` call inside `flushAudit()`).
  const FLUSH_MARKER_KEY = `__attachWriteSetAudit_flush_marker_${Math.random().toString(36).slice(2)}__`
  const pendingFlushWaiters: Array<() => void> = []

  client.on('DOMStorage.domStorageItemAdded', (event) => {
    if (!event.storageId.isLocalStorage) return
    if (event.key === FLUSH_MARKER_KEY) return
    record('setItem', event.key)
  })
  client.on('DOMStorage.domStorageItemUpdated', (event) => {
    if (!event.storageId.isLocalStorage) return
    if (event.key === FLUSH_MARKER_KEY) return
    record('setItem', event.key)
  })
  client.on('DOMStorage.domStorageItemRemoved', (event) => {
    if (!event.storageId.isLocalStorage) return
    if (event.key === FLUSH_MARKER_KEY) {
      // Causal drain signal for flushAudit() — see module-level doc comment
      // and flushAudit()'s own comment below. Not a recorded operation.
      const waiter = pendingFlushWaiters.shift()
      waiter?.()
      return
    }
    record('removeItem', event.key)
  })
  client.on('DOMStorage.domStorageItemsCleared', (event) => {
    if (event.storageId.isLocalStorage) record('clear', null)
  })

  // OWNER REQUEST_CHANGES repair (PR #1989 review, issuecomment-5179331591,
  // P1 blocker): see the module-level `flushAudit()` contract doc comment
  // above for the full causal-drain rationale. In short: a bare CDP command
  // round-trip proves only that events already in flight to this session
  // have arrived, not that a JS write which just returned has already
  // become an event. Writing-then-deleting a same-page/origin marker key
  // and waiting for the marker's OWN removal event to arrive gives a
  // genuine causal boundary, because Chromium processes pending storage
  // mutations from a given source in FIFO order.
  async function flushAudit(): Promise<void> {
    const markerObserved = new Promise<void>((resolve, reject) => {
      const timeoutId = setTimeout(() => {
        reject(
          new Error(
            'flushAudit: timed out waiting for the internal drain marker\'s ' +
              'DOMStorage.domStorageItemRemoved event (this indicates a CDP ' +
              'DOMStorage event delivery problem, not a normal test failure)',
          ),
        )
      }, 5_000)
      pendingFlushWaiters.push(() => {
        clearTimeout(timeoutId)
        resolve()
      })
    })

    await page.evaluate((markerKey: string) => {
      window.localStorage.setItem(markerKey, '1')
      window.localStorage.removeItem(markerKey)
    }, FLUSH_MARKER_KEY)

    await markerObserved
  }

  return { writeLog, flushAudit }
}
