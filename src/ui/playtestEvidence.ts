/**
 * playtestEvidence.ts
 *
 * GitHub Pages / PR Preview 上で opt-in（?playtest_evidence=1）表示する
 * プレイテスト証跡エクスポートパネル。
 *
 * - read-only UI: src/state / src/systems / src/storage を変更しない (AC11)
 * - DOM への読み取りアクセスのみ
 */

// ---------------------------------------------------------------------------
// navigator.userAgentData は実験的 API のため型宣言が DOM lib に含まれない。
// AC4: getHighEntropyValues() fallback のために最小限の型を宣言する。
// ---------------------------------------------------------------------------

interface NavigatorUAData {
  platform: string
  getHighEntropyValues(hints: string[]): Promise<{
    fullVersionList?: Array<{ brand: string; version: string }>
    uaFullVersion?: string
    platformVersion?: string
  }>
}

declare global {
  interface Navigator {
    userAgentData?: NavigatorUAData
  }
}

// ---------------------------------------------------------------------------
// Types (AC8)
// ---------------------------------------------------------------------------

export interface ViewportMetrics {
  inner_width: number
  inner_height: number
  client_width: number
  client_height: number
  visual_viewport_width: number | null
  visual_viewport_height: number | null
  visual_viewport_scale: number | null
}

export interface DevicePixelRatio {
  value: number
  note: string
}

export interface ScreenMetrics {
  width: number
  height: number
  avail_width: number
  avail_height: number
}

export interface BrowserInfo {
  /** Full version string or 'unknown' */
  version: string
  /** How the version was obtained */
  version_source: 'userAgentData' | 'userAgent' | 'unknown'
  /** Present only when version_source is 'unknown' */
  unknown_reason?: string
  platform: string
  user_agent: string
}

export interface AppUnderTest {
  name: string
  commit: string
  commit_unknown_reason?: string
}

export interface EnvironmentInfo {
  viewport: ViewportMetrics
  device_pixel_ratio: DevicePixelRatio
  screen: ScreenMetrics
  timezone: string
  language: string
}

export interface PlaytestEvidenceData {
  playtest_evidence_schema_version: 'v1'
  generated_at: string
  source_url: string
  app_under_test: AppUnderTest
  browser: BrowserInfo
  environment: EnvironmentInfo
  hashes: Record<string, string>
}


// ---------------------------------------------------------------------------
// App Under Test commit resolution (AC3, AC4, AC5)
// ---------------------------------------------------------------------------

const SHA_REGEX = /^[0-9a-f]{40}$/

/**
 * Resolve the commit SHA for the app under test.
 *
 * AC3: Returns 40-char hex SHA when VITE_LOOP_COMMIT_SHA is injected at build time.
 * AC4: Falls back to commit: "unknown" with commit_unknown_reason when unset or invalid.
 * AC5: commit_unknown_reason key is absent (object shape branched) on success.
 */
/**
 * @param overrideEnvValue - Injectable for unit testing only. Pass undefined in production.
 *   In production the value comes from import.meta.env.VITE_LOOP_COMMIT_SHA (Vite build-time replace).
 */
export function resolveAppUnderTestCommit(overrideEnvValue?: string | null): AppUnderTest {
  // import.meta.env.VITE_LOOP_COMMIT_SHA is replaced at build time by Vite.
  // overrideEnvValue is used only in unit tests to inject a controlled value.
  const raw: string | undefined =
    overrideEnvValue !== undefined && overrideEnvValue !== null
      ? overrideEnvValue
      : typeof import.meta !== 'undefined' && import.meta.env
        ? import.meta.env.VITE_LOOP_COMMIT_SHA
        : undefined

  if (raw !== undefined && SHA_REGEX.test(raw)) {
    // AC5: object shape has no commit_unknown_reason key on success
    return {
      name: 'loop-protocol',
      commit: raw,
    }
  }

  // AC4: fallback branch — include commit_unknown_reason
  const reason =
    raw === undefined
      ? 'VITE_LOOP_COMMIT_SHA was not set at build time'
      : `VITE_LOOP_COMMIT_SHA value "${raw}" is not a valid 40-character hex SHA`

  return {
    name: 'loop-protocol',
    commit: 'unknown',
    commit_unknown_reason: reason,
  }
}

// ---------------------------------------------------------------------------
// Browser info collection (AC3, AC4)
// ---------------------------------------------------------------------------

function collectBrowserInfo(): BrowserInfo {
  const ua = typeof navigator !== 'undefined' ? navigator.userAgent : ''
  const platform =
    typeof navigator !== 'undefined'
      ? (navigator.userAgentData?.platform ?? navigator.platform ?? 'unknown')
      : 'unknown'

  // AC4: synchronous initial collection -- parses userAgent as best-effort.
  // Chrome full version is obtained asynchronously via collectBrowserInfoAsync().
  const versionFromUA = parseVersionFromUA(ua)

  if (versionFromUA) {
    return {
      version: versionFromUA,
      version_source: 'userAgent',
      platform,
      user_agent: ua,
    }
  }

  return {
    version: 'unknown',
    version_source: 'unknown',
    unknown_reason:
      'navigator.userAgent did not contain a recognizable version token; getHighEntropyValues will be attempted asynchronously',
    platform,
    user_agent: ua,
  }
}

/**
 * AC4: Async browser info collection using getHighEntropyValues.
 * Attempts to obtain the full Chrome version (e.g. "124.0.6367.82") via the
 * User-Agent Client Hints API. Falls back to userAgent parse on failure.
 */
async function collectBrowserInfoAsync(): Promise<BrowserInfo> {
  const ua = typeof navigator !== 'undefined' ? navigator.userAgent : ''
  const platform =
    typeof navigator !== 'undefined'
      ? (navigator.userAgentData?.platform ?? navigator.platform ?? 'unknown')
      : 'unknown'

  if (
    typeof navigator !== 'undefined' &&
    navigator.userAgentData &&
    typeof navigator.userAgentData.getHighEntropyValues === 'function'
  ) {
    try {
      const hints = await navigator.userAgentData.getHighEntropyValues([
        'fullVersionList',
        'uaFullVersion',
        'platformVersion',
      ])
      // Prefer fullVersionList for Chromium-based browsers (highest fidelity)
      if (hints.fullVersionList && hints.fullVersionList.length > 0) {
        // Pick the entry with the longest version string (most specific)
        const best = hints.fullVersionList.reduce((a, b) =>
          a.version.length >= b.version.length ? a : b,
        )
        if (best.version && !best.brand.includes('Not')) {
          return {
            version: best.version,
            version_source: 'userAgentData',
            platform,
            user_agent: ua,
          }
        }
      }
      if (hints.uaFullVersion) {
        return {
          version: hints.uaFullVersion,
          version_source: 'userAgentData',
          platform,
          user_agent: ua,
        }
      }
    } catch {
      // getHighEntropyValues rejected -- fall through to userAgent parse
    }
  }

  // Fallback: parse userAgent
  const versionFromUA = parseVersionFromUA(ua)
  if (versionFromUA) {
    return {
      version: versionFromUA,
      version_source: 'userAgent',
      platform,
      user_agent: ua,
    }
  }

  return {
    version: 'unknown',
    version_source: 'unknown',
    unknown_reason:
      'getHighEntropyValues unavailable or rejected, and navigator.userAgent did not contain a recognizable version token',
    platform,
    user_agent: ua,
  }
}

/** Parse a best-effort version string from a User-Agent string. */
function parseVersionFromUA(ua: string): string | null {
  if (!ua) return null
  // Chrome/Chromium
  const chrome = /Chrome\/(\S+)/.exec(ua)
  if (chrome) return chrome[1]
  // Firefox
  const firefox = /Firefox\/(\S+)/.exec(ua)
  if (firefox) return firefox[1]
  // Safari (must be after Chrome because Chrome also has Safari/ in UA)
  const safari = /Version\/(\S+).*Safari/.exec(ua)
  if (safari) return safari[1]
  // Edge (legacy)
  const edge = /Edge\/(\S+)/.exec(ua)
  if (edge) return edge[1]
  return null
}

// ---------------------------------------------------------------------------
// Viewport / DPR / Screen collection (AC5, AC6, AC7)
// ---------------------------------------------------------------------------

function collectViewport(): ViewportMetrics {
  if (typeof window === 'undefined') {
    return {
      inner_width: 0,
      inner_height: 0,
      client_width: 0,
      client_height: 0,
      visual_viewport_width: null,
      visual_viewport_height: null,
      visual_viewport_scale: null,
    }
  }
  const vv = window.visualViewport
  return {
    inner_width: window.innerWidth,
    inner_height: window.innerHeight,
    client_width: document.documentElement.clientWidth,
    client_height: document.documentElement.clientHeight,
    visual_viewport_width: vv ? vv.width : null,
    visual_viewport_height: vv ? vv.height : null,
    visual_viewport_scale: vv ? vv.scale : null,
  }
}

function collectDevicePixelRatio(): DevicePixelRatio {
  const value = typeof window !== 'undefined' ? (window.devicePixelRatio ?? 1) : 1
  return {
    value,
    note: 'device_pixel_ratio はページズームおよび OS の display scaling 設定によって変化します。1.0 が物理ピクセル等倍とは限りません。',
  }
}

function collectScreen(): ScreenMetrics {
  if (typeof screen === 'undefined') {
    return { width: 0, height: 0, avail_width: 0, avail_height: 0 }
  }
  return {
    width: screen.width,
    height: screen.height,
    avail_width: screen.availWidth,
    avail_height: screen.availHeight,
  }
}

// ---------------------------------------------------------------------------
// Main data builder (AC3, AC5-AC8)
// ---------------------------------------------------------------------------

export function buildEvidenceData(): PlaytestEvidenceData {
  const now = new Date()
  const generated_at = now.toISOString()

  const source_url = typeof location !== 'undefined' ? location.href : 'unknown'

  const app_under_test = resolveAppUnderTestCommit()

  const browser = collectBrowserInfo()

  const environment: EnvironmentInfo = {
    viewport: collectViewport(),
    device_pixel_ratio: collectDevicePixelRatio(),
    screen: collectScreen(),
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    language: typeof navigator !== 'undefined' ? navigator.language : 'unknown',
  }

  return {
    playtest_evidence_schema_version: 'v1',
    generated_at,
    source_url,
    app_under_test,
    browser,
    environment,
    hashes: {},
  }
}

// ---------------------------------------------------------------------------
// YAML serializer (AC8)
// ---------------------------------------------------------------------------

function yamlValue(val: unknown, indent: number): string {
  const pad = '  '.repeat(indent)
  if (val === null || val === undefined) return 'null'
  if (typeof val === 'boolean') return String(val)
  if (typeof val === 'number') return String(val)
  if (typeof val === 'string') {
    // Quote strings that contain special chars or look like YAML keywords
    if (
      val.includes(':') ||
      val.includes('#') ||
      val.includes('\n') ||
      val === 'true' ||
      val === 'false' ||
      val === 'null' ||
      val === 'unknown'
    ) {
      return '"' + val.replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/\n/g, '\\n') + '"'
    }
    return val
  }
  if (Array.isArray(val)) {
    if (val.length === 0) return '[]'
    return '\n' + val.map((v) => pad + '- ' + yamlValue(v, indent + 1)).join('\n')
  }
  if (typeof val === 'object') {
    const entries = Object.entries(val as Record<string, unknown>)
    if (entries.length === 0) return '{}'
    return (
      '\n' +
      entries
        .map(([k, v]) => {
          const vStr = yamlValue(v, indent + 1)
          if (vStr.startsWith('\n')) {
            return pad + k + ':' + vStr
          }
          return pad + k + ': ' + vStr
        })
        .join('\n')
    )
  }
  return String(val)
}

export function toYaml(data: PlaytestEvidenceData): string {
  const lines: string[] = []
  lines.push('# Loop Protocol Playtest Evidence')
  lines.push('# Generated by playtestEvidence panel (AC8)')
  lines.push('')
  for (const [key, val] of Object.entries(data)) {
    const vStr = yamlValue(val, 1)
    if (vStr.startsWith('\n')) {
      lines.push(key + ':' + vStr)
    } else {
      lines.push(key + ': ' + vStr)
    }
  }
  return lines.join('\n') + '\n'
}

// ---------------------------------------------------------------------------
// Panel DOM (AC2, AC9, AC10)
// ---------------------------------------------------------------------------

/** Filename for downloaded YAML (AC10) */
function evidenceFilename(generatedAt: string): string {
  // ISO 8601 with colons replaced for filename safety
  const safe = generatedAt.replace(/:/g, '-').replace(/\./g, '-')
  return 'loop-protocol-playtest-evidence-' + safe + '.yaml'
}

/** Download a string as a file (AC10) */
function downloadString(content: string, filename: string): void {
  const blob = new Blob([content], { type: 'text/yaml' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

interface MountPanelResult {
  panel: HTMLElement
  /** Call once when the panel is first shown to lock the evidence snapshot. */
  initSnapshot: () => void
}

/** Build and mount the Evidence Panel DOM node (AC2, AC9, AC10) */
function mountPanel(container: HTMLElement, initiallyHidden: boolean): MountPanelResult {
  // AC12: lazy-initialize snapshot on first open.
  // buildEvidenceData() is NOT called here (mount time); it is called the first time
  // the toggle opens the panel so that viewport/generated_at reflect the actual open moment.
  // Once locked, the same snapshot is reused across close/reopen cycles.
  let snapshotData: ReturnType<typeof buildEvidenceData> | null = null
  // Mutable reference so async update can refresh textarea / download
  let currentYaml: string = ''

  const panelId = 'playtest-evidence-panel'

  const panel = document.createElement('aside')
  panel.id = panelId
  panel.setAttribute('data-playtest-evidence', 'true')
  panel.hidden = initiallyHidden
  panel.style.cssText = [
    'position:fixed',
    'top:0',
    'right:0',
    'width:420px',
    'max-height:100vh',
    'overflow-y:auto',
    'background:#1a1a2e',
    'color:#e0e0e0',
    'font-family:monospace',
    'font-size:12px',
    'padding:16px',
    'z-index:9999',
    'box-sizing:border-box',
    'border-left:2px solid #4a9eff',
  ].join(';')

  // --- AC11/AC3: Close button ---
  const closeBtn = document.createElement('button')
  closeBtn.type = 'button'
  closeBtn.setAttribute('data-playtest-close', 'true')
  closeBtn.setAttribute('aria-label', 'Close Playtest Evidence Panel')
  closeBtn.textContent = '×'
  closeBtn.style.cssText = [
    'position:absolute',
    'top:8px',
    'right:8px',
    'background:transparent',
    'border:none',
    'color:#e0e0e0',
    'font-size:18px',
    'cursor:pointer',
    'padding:2px 6px',
    'line-height:1',
  ].join(';')

  const title = document.createElement('h2')
  title.textContent = 'Playtest Evidence Panel'
  title.style.cssText = 'margin:0 0 8px;font-size:14px;color:#4a9eff'

  const hint = document.createElement('p')
  hint.style.cssText = 'margin:0 0 12px;color:#aaa;font-size:11px'
  hint.textContent =
    'AC: ?playtest_evidence=1 で有効化。ゲーム状態を変更しません。'

  // Textarea fallback for manual copy (AC9 fallback)
  const textarea = document.createElement('textarea')
  textarea.setAttribute('data-playtest-fallback', 'true')
  textarea.value = ''
  textarea.readOnly = true
  textarea.style.cssText = [
    'width:100%',
    'height:200px',
    'background:#0d0d1a',
    'color:#c8ffb0',
    'border:1px solid #333',
    'padding:8px',
    'font-size:10px',
    'box-sizing:border-box',
    'resize:vertical',
    'margin-bottom:8px',
  ].join(';')

  // Copy YAML button (AC9)
  const copyBtn = document.createElement('button')
  copyBtn.setAttribute('data-action', 'copy-yaml')
  copyBtn.textContent = 'Copy YAML to Clipboard'
  copyBtn.style.cssText =
    'margin-right:8px;padding:6px 12px;background:#4a9eff;color:#000;border:none;cursor:pointer;font-size:12px'
  copyBtn.addEventListener('click', () => {
    if (navigator.clipboard) {
      navigator.clipboard.writeText(currentYaml).then(
        () => {
          copyBtn.textContent = 'Copied!'
          setTimeout(() => {
            copyBtn.textContent = 'Copy YAML to Clipboard'
          }, 2000)
        },
        () => {
          // Clipboard API failed -- textarea fallback is already visible
          copyBtn.textContent = 'Copy failed -- use textarea below'
        },
      )
    } else {
      // No Clipboard API -- textarea fallback
      textarea.select()
      copyBtn.textContent = 'Use textarea to copy manually'
    }
  })

  // Download YAML button (AC10)
  const downloadBtn = document.createElement('button')
  downloadBtn.setAttribute('data-action', 'download-yaml')
  downloadBtn.textContent = 'Download YAML'
  downloadBtn.style.cssText =
    'padding:6px 12px;background:#2a6e2a;color:#fff;border:none;cursor:pointer;font-size:12px'
  downloadBtn.addEventListener('click', () => {
    if (snapshotData) {
      downloadString(currentYaml, evidenceFilename(snapshotData.generated_at))
    }
  })

  const btnRow = document.createElement('div')
  btnRow.style.cssText = 'margin-bottom:12px'
  btnRow.appendChild(copyBtn)
  btnRow.appendChild(downloadBtn)

  panel.appendChild(closeBtn)
  panel.appendChild(title)
  panel.appendChild(hint)
  panel.appendChild(btnRow)
  panel.appendChild(textarea)

  container.appendChild(panel)

  // AC4: async browser version enrichment.
  // We store the async browser result so it can be applied when the snapshot is first generated.
  let asyncBrowserCache: Awaited<ReturnType<typeof collectBrowserInfoAsync>> | null = null
  collectBrowserInfoAsync().then((asyncBrowser) => {
    asyncBrowserCache = asyncBrowser
    // If snapshot was already generated (panel opened before async resolved), refresh it.
    if (snapshotData && asyncBrowser.version_source === 'userAgentData') {
      const updatedData: PlaytestEvidenceData = { ...snapshotData, browser: asyncBrowser }
      snapshotData = updatedData
      currentYaml = toYaml(updatedData)
      textarea.value = currentYaml
    }
  }).catch(() => {
    // Async update failed silently -- sync render remains
  })

  /**
   * AC12: Initialize the evidence snapshot on first open.
   * Subsequent calls are no-ops (snapshot is locked after first call).
   */
  function initSnapshot(): void {
    if (snapshotData !== null) return  // already locked
    let data = buildEvidenceData()
    // Apply async browser info if already resolved
    if (asyncBrowserCache && asyncBrowserCache.version_source === 'userAgentData') {
      data = { ...data, browser: asyncBrowserCache }
    }
    snapshotData = data
    currentYaml = toYaml(data)
    textarea.value = currentYaml
  }

  return { panel, initSnapshot }
}

/** Build and mount the always-visible toggle button (AC1, AC2, AC10) */
function mountToggle(
  container: HTMLElement,
  panel: HTMLElement,
  initSnapshot: () => void,
): HTMLElement {
  const panelId = panel.id || 'playtest-evidence-panel'

  const toggleBtn = document.createElement('button')
  toggleBtn.type = 'button'
  toggleBtn.setAttribute('data-playtest-toggle', 'true')
  toggleBtn.setAttribute('aria-controls', panelId)
  toggleBtn.setAttribute('aria-expanded', panel.hidden ? 'false' : 'true')
  toggleBtn.setAttribute('aria-label', 'Open Playtest Evidence Panel')
  toggleBtn.textContent = 'Evidence'
  toggleBtn.style.cssText = [
    'position:fixed',
    'bottom:8px',
    'right:8px',
    'background:#1a1a2e',
    'color:#4a9eff',
    'border:1px solid #4a9eff',
    'padding:4px 10px',
    'font-size:11px',
    'font-family:monospace',
    'cursor:pointer',
    'z-index:9998',
    'box-sizing:border-box',
  ].join(';')

  // Wire up toggle button click: show/hide panel, update aria-expanded
  // AC12: call initSnapshot() when opening so snapshot is lazy-initialized on first open.
  toggleBtn.addEventListener('click', () => {
    panel.hidden = !panel.hidden
    toggleBtn.setAttribute('aria-expanded', panel.hidden ? 'false' : 'true')
    if (!panel.hidden) {
      initSnapshot()
    }
  })

  // Wire up close button inside panel: hide panel, update toggle aria-expanded
  const closeBtn = panel.querySelector('[data-playtest-close="true"]') as HTMLButtonElement | null
  if (closeBtn) {
    closeBtn.addEventListener('click', () => {
      panel.hidden = true
      toggleBtn.setAttribute('aria-expanded', 'false')
    })
  }

  container.appendChild(toggleBtn)
  return toggleBtn
}

/**
 * Pure predicate: should the Evidence Panel be shown for the given URL search string?
 *
 * AC2: returns true only when playtest_evidence=1 is present.
 * Exported for unit testing without DOM dependency.
 */
export function shouldShowPanel(search: string): boolean {
  const params = new URLSearchParams(search)
  return params.get('playtest_evidence') === '1'
}

/**
 * Initialize the playtest evidence panel.
 *
 * Always mounts the toggle button (AC1, AC2).
 * Panel body is initially hidden unless search contains playtest_evidence=1 (AC5, AC8, AC9).
 * Calling this function multiple times is idempotent: subsequent calls are no-ops (AC13).
 *
 * @param container - DOM element to mount the panel into
 * @param search    - URL search string (e.g. location.search). Defaults to location.search if omitted.
 */
export function initPlaytestEvidencePanel(
  container: HTMLElement,
  search?: string,
): void {
  // AC13: idempotent -- do not mount more than once per container
  if (container.querySelector('[data-playtest-toggle="true"]')) {
    return
  }

  const q = search ?? (typeof location !== 'undefined' ? location.search : '')
  const panelOpen = shouldShowPanel(q)

  // Mount panel (initially hidden unless ?playtest_evidence=1)
  const { panel, initSnapshot } = mountPanel(container, !panelOpen)

  // AC12: if panel is initially open (e.g. ?playtest_evidence=1), initialize snapshot immediately.
  if (panelOpen) {
    initSnapshot()
  }

  // Always mount the toggle button
  mountToggle(container, panel, initSnapshot)
}
