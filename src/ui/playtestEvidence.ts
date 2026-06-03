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
  commit: 'unknown'
  commit_unknown_reason: string
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
// Browser info collection (AC3, AC4)
// ---------------------------------------------------------------------------

function collectBrowserInfo(): BrowserInfo {
  const ua = typeof navigator !== 'undefined' ? navigator.userAgent : ''
  const platform =
    typeof navigator !== 'undefined'
      ? (navigator.userAgentData?.platform ?? navigator.platform ?? 'unknown')
      : 'unknown'

  // AC4: synchronous initial collection — parses userAgent as best-effort.
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
      // getHighEntropyValues rejected — fall through to userAgent parse
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

  const app_under_test: AppUnderTest = {
    name: 'loop-protocol',
    commit: 'unknown',
    commit_unknown_reason:
      'GitHub Pages / PR Preview 上のブラウザからは build-time commit SHA を確定できません。commit SHA の注入には別途 workflow 対応（別 Issue）が必要です。',
  }

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
      return `"${val.replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/\n/g, '\\n')}"`
    }
    return val
  }
  if (Array.isArray(val)) {
    if (val.length === 0) return '[]'
    return '\n' + val.map((v) => `${pad}- ${yamlValue(v, indent + 1)}`).join('\n')
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
            return `${pad}${k}:${vStr}`
          }
          return `${pad}${k}: ${vStr}`
        })
        .join('\n')
    )
  }
  return String(val)
}

export function toYaml(data: PlaytestEvidenceData): string {
  const lines: string[] = []
  lines.push(`# Loop Protocol Playtest Evidence`)
  lines.push(`# Generated by playtestEvidence panel (AC8)`)
  lines.push(``)
  for (const [key, val] of Object.entries(data)) {
    const vStr = yamlValue(val, 1)
    if (vStr.startsWith('\n')) {
      lines.push(`${key}:${vStr}`)
    } else {
      lines.push(`${key}: ${vStr}`)
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
  return `loop-protocol-playtest-evidence-${safe}.yaml`
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

/** Build and mount the Evidence Panel DOM node (AC2, AC9, AC10) */
function mountPanel(container: HTMLElement): void {
  // Initial synchronous render — browser version may be userAgent-parsed initially
  const data = buildEvidenceData()
  // Mutable reference so async update can refresh textarea / download
  let currentYaml = toYaml(data)

  const panel = document.createElement('aside')
  panel.setAttribute('data-playtest-evidence', 'true')
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
  textarea.value = currentYaml
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
          // Clipboard API failed — textarea fallback is already visible
          copyBtn.textContent = 'Copy failed — use textarea below'
        },
      )
    } else {
      // No Clipboard API — textarea fallback
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
    downloadString(currentYaml, evidenceFilename(data.generated_at))
  })

  const btnRow = document.createElement('div')
  btnRow.style.cssText = 'margin-bottom:12px'
  btnRow.appendChild(copyBtn)
  btnRow.appendChild(downloadBtn)

  panel.appendChild(title)
  panel.appendChild(hint)
  panel.appendChild(btnRow)
  panel.appendChild(textarea)

  container.appendChild(panel)

  // AC4: async update — fetch high-entropy browser version and refresh panel content
  collectBrowserInfoAsync().then((asyncBrowser) => {
    if (asyncBrowser.version_source === 'userAgentData') {
      // Replace browser info in data and re-render YAML
      const updatedData: PlaytestEvidenceData = { ...data, browser: asyncBrowser }
      currentYaml = toYaml(updatedData)
      textarea.value = currentYaml
    }
  }).catch(() => {
    // Async update failed silently — initial sync render remains
  })
}

/**
 * Pure predicate: should the Evidence Panel be shown for the given URL search string?
 *
 * AC2: returns true only when `playtest_evidence=1` is present.
 * Exported for unit testing without DOM dependency.
 */
export function shouldShowPanel(search: string): boolean {
  const params = new URLSearchParams(search)
  return params.get('playtest_evidence') === '1'
}

/**
 * Initialize the playtest evidence panel.
 *
 * @param container - DOM element to mount the panel into
 * @param search    - URL search string (e.g. location.search). Defaults to location.search if omitted.
 *
 * AC2: panel is shown only when search contains `playtest_evidence=1`.
 */
export function initPlaytestEvidencePanel(
  container: HTMLElement,
  search?: string,
): void {
  const q = search ?? (typeof location !== 'undefined' ? location.search : '')
  if (shouldShowPanel(q)) {
    mountPanel(container)
  }
}
