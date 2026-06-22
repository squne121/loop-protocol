/**
 * playtestEvidence.ts
 *
 * GitHub Pages / PR Preview 上で opt-in（?playtest_evidence=1）表示する
 * プレイテスト証跡エクスポートパネル。
 *
 * - DOM / 環境 metadata の読み取りに専念する UI レイヤ。
 * - 証跡の runtime snapshot は注入された `getSnapshot` callback 経由で取得する
 *   (B1, #987)。module-global store には依存しない。
 * - self-explanation の保存は注入された `onSaveExplanation` callback を通じて
 *   state-scoped recorder API に委譲する (B1/B6, #987)。UI 自身は src/state を
 *   直接書き換えない。
 */

import {
  type AssistPlayerPlaytestEvent,
  type AssistPlayerRuntimeEvidenceSnapshot,
  type AssistPlayerTerminalState,
  type QualitativeNotes,
} from '../playtest/assistPlayerEventLog'

/**
 * Callbacks injected into the evidence panel so it can read the live runtime
 * snapshot and persist self-explanation text without importing a module-global
 * store (B1, #987).
 */
export interface PlaytestEvidencePanelDeps {
  /** Returns the current state-scoped evidence snapshot. */
  getSnapshot: () => AssistPlayerRuntimeEvidenceSnapshot
  /** Persists the self-explanation response via the state-scoped recorder. */
  onSaveExplanation: (response: string) => void
}

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
  declared_browser_zoom: {
    declared_percent: 100 | 125 | 150 | 200 | 'manual_unknown'
    source: 'test_matrix' | 'manual_input' | 'unknown'
  }
}

/**
 * Explicit unfulfilled-provenance markers (B2, #987).
 *
 * Any value carrying one of these `availability_reason`s is NOT a satisfied
 * provenance field — a placeholder must never be treated as achieved evidence.
 * - `unknown`: value was not injected at build time (local dev / unset env).
 * - `unavailable-in-deploy-pages`: deploy-pages flow does not produce this value.
 * - `manual_capture_required`: requires an out-of-band manual capture step.
 * - `unavailable-in-bundle-build-time`: only known *after* the build (artifact
 *   url / digest / retention); emitted by a separate workflow step, never the bundle.
 */
export type ProvenanceAvailabilityReason =
  | 'available'
  | 'unknown'
  | 'unavailable-in-deploy-pages'
  | 'manual_capture_required'
  | 'unavailable-in-bundle-build-time'

const UNFULFILLED_PROVENANCE_REASONS: ReadonlySet<string> = new Set([
  'unknown',
  'unavailable-in-deploy-pages',
  'manual_capture_required',
  'unavailable-in-bundle-build-time',
])

/** A provenance field value plus an explicit availability_reason (B2, #987). */
export interface ProvenanceField {
  value: string
  availability_reason: ProvenanceAvailabilityReason
}

/**
 * Returns true iff the provenance field is genuinely satisfied (i.e. its
 * availability_reason is not one of the unfulfilled markers). Exported for
 * tests (B8 #5).
 */
export function isProvenanceFulfilled(field: ProvenanceField): boolean {
  return !UNFULFILLED_PROVENANCE_REASONS.has(field.availability_reason)
}

export interface ArtifactMetadata {
  run_id: ProvenanceField
  run_attempt: ProvenanceField
  page_url: ProvenanceField
  artifact_url: ProvenanceField
  artifact_names: string[]
  artifact_digest_or_attestation: ProvenanceField
  retention_days: ProvenanceField
  screenshot_path: ProvenanceField
}

export interface RuntimeEvidenceState {
  sortie_id: string
  paused_or_running: 'paused' | 'running'
  terminal_state: AssistPlayerTerminalState
}

export interface PlaytestEvidenceData {
  playtest_evidence_schema_version: 'v2'
  generated_at: string
  source_url: string
  app_under_test: AppUnderTest
  browser: BrowserInfo
  environment: EnvironmentInfo
  artifacts: ArtifactMetadata
  runtime_state: RuntimeEvidenceState
  deterministic_events: AssistPlayerPlaytestEvent[]
  qualitative_notes?: QualitativeNotes
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

function readBuildEnv(key: keyof ImportMetaEnv): string {
  const value =
    typeof import.meta !== 'undefined' && import.meta.env
      ? import.meta.env[key]
      : undefined
  return typeof value === 'string' && value.length > 0 ? value : 'unknown'
}

/**
 * Reads a provenance env value and classifies it (B2, #987).
 *
 * A raw value matching one of the unfulfilled markers becomes the field's
 * availability_reason (and is treated as NOT achieved). Otherwise the field is
 * `available` with the real injected value.
 *
 * @param forcedReason When provided, the field is always emitted with this
 *   unfulfilled reason regardless of the raw value (used for build-after fields
 *   that the bundle can never carry, e.g. artifact url/digest/retention).
 */
function readProvenanceField(
  key: keyof ImportMetaEnv,
  forcedReason?: Exclude<ProvenanceAvailabilityReason, 'available'>,
): ProvenanceField {
  const raw = readBuildEnv(key)
  if (forcedReason !== undefined) {
    return { value: raw, availability_reason: forcedReason }
  }
  if (UNFULFILLED_PROVENANCE_REASONS.has(raw)) {
    return { value: raw, availability_reason: raw as ProvenanceAvailabilityReason }
  }
  return { value: raw, availability_reason: 'available' }
}

function collectDeclaredBrowserZoom(): EnvironmentInfo['declared_browser_zoom'] {
  return {
    declared_percent: 'manual_unknown',
    source: 'unknown',
  }
}

function collectArtifactMetadata(): ArtifactMetadata {
  return {
    // Build-time confirmable values: real value injected via workflow env.
    run_id: readProvenanceField('VITE_LOOP_RUN_ID'),
    run_attempt: readProvenanceField('VITE_LOOP_RUN_ATTEMPT'),
    page_url: readProvenanceField('VITE_LOOP_PAGE_URL'),
    artifact_names: readBuildEnv('VITE_LOOP_ARTIFACT_NAMES')
      .split(',')
      .map((entry) => entry.trim())
      .filter((entry) => entry.length > 0 && entry !== 'unknown'),
    // Build-after values: never knowable at bundle build time. The bundle marks
    // them unfulfilled; the real values are emitted by a separate workflow step
    // into playtest-evidence-provenance.json (B2, #987).
    artifact_url: readProvenanceField(
      'VITE_LOOP_ARTIFACT_URL',
      'unavailable-in-bundle-build-time',
    ),
    artifact_digest_or_attestation: readProvenanceField(
      'VITE_LOOP_ARTIFACT_DIGEST_OR_ATTESTATION',
      'unavailable-in-bundle-build-time',
    ),
    retention_days: readProvenanceField(
      'VITE_LOOP_RETENTION_DAYS',
      'unavailable-in-bundle-build-time',
    ),
    screenshot_path: readProvenanceField('VITE_LOOP_SCREENSHOT_PATH'),
  }
}

/**
 * Test-only accessor for provenance classification (B2/B8 #5, #987). Production
 * code uses collectArtifactMetadata via buildEvidenceData.
 */
export function collectArtifactMetadataForTest(): ArtifactMetadata {
  return collectArtifactMetadata()
}

function collectPausedOrRunningState(): RuntimeEvidenceState['paused_or_running'] {
  if (typeof document === 'undefined') {
    return 'running'
  }
  const togglePauseButton = document.querySelector('[data-action="toggle-pause"]')
  if (togglePauseButton?.getAttribute('aria-pressed') === 'true') {
    return 'paused'
  }
  return 'running'
}

// ---------------------------------------------------------------------------
// Main data builder (AC3, AC5-AC8)
// ---------------------------------------------------------------------------

/**
 * Builds the evidence payload from a *provided* runtime snapshot (B1, #987).
 *
 * @param runtimeEvidence State-scoped snapshot from the injected getSnapshot
 *   callback. Defaults to an empty/uninitialized snapshot so pure unit tests can
 *   call buildEvidenceData() with no arguments.
 * @param generatedAtOverride Fixed timestamp for snapshot stability (B6).
 */
const EMPTY_RUNTIME_SNAPSHOT: AssistPlayerRuntimeEvidenceSnapshot = {
  sortie_id: 'sortie-uninitialized',
  terminal_state: 'running',
  deterministic_events: [],
}

export function buildEvidenceData(
  runtimeEvidence: AssistPlayerRuntimeEvidenceSnapshot = EMPTY_RUNTIME_SNAPSHOT,
  generatedAtOverride?: string,
): PlaytestEvidenceData {
  const generated_at = generatedAtOverride ?? new Date().toISOString()

  const source_url = typeof location !== 'undefined' ? location.href : 'unknown'

  const app_under_test = resolveAppUnderTestCommit()

  const browser = collectBrowserInfo()

  const environment: EnvironmentInfo = {
    viewport: collectViewport(),
    device_pixel_ratio: collectDevicePixelRatio(),
    screen: collectScreen(),
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    language: typeof navigator !== 'undefined' ? navigator.language : 'unknown',
    declared_browser_zoom: collectDeclaredBrowserZoom(),
  }

  return {
    playtest_evidence_schema_version: 'v2',
    generated_at,
    source_url,
    app_under_test,
    browser,
    environment,
    artifacts: collectArtifactMetadata(),
    runtime_state: {
      sortie_id: runtimeEvidence.sortie_id,
      paused_or_running: collectPausedOrRunningState(),
      terminal_state: runtimeEvidence.terminal_state,
    },
    deterministic_events: runtimeEvidence.deterministic_events,
    qualitative_notes: runtimeEvidence.qualitative_notes,
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
  refreshSnapshot: () => void
}

interface PromptMountResult {
  card: HTMLElement
  syncPrompt: () => void
}

function mountSelfExplanationPrompt(
  container: HTMLElement,
  deps: PlaytestEvidencePanelDeps,
): PromptMountResult {
  const card = document.createElement('section')
  card.setAttribute('data-self-explanation-card', 'true')
  card.hidden = true
  card.style.cssText = [
    'position:fixed',
    'left:8px',
    'bottom:8px',
    'width:min(360px, calc(100vw - 16px))',
    'background:#101820',
    'color:#f5f7fa',
    'border:1px solid #5cc8ff',
    'padding:12px',
    'z-index:9997',
    'box-sizing:border-box',
  ].join(';')

  const title = document.createElement('p')
  title.textContent = 'Post-sortie self-explanation'
  title.style.cssText = 'margin:0 0 8px;font-weight:bold'

  const prompt = document.createElement('p')
  prompt.setAttribute('data-self-explanation-prompt', 'true')
  prompt.setAttribute('role', 'status')
  prompt.setAttribute('aria-live', 'polite')
  prompt.style.cssText = 'margin:0 0 8px'

  const response = document.createElement('textarea')
  response.setAttribute('data-self-explanation-response', 'true')
  response.rows = 4
  response.style.cssText = 'width:100%;box-sizing:border-box;margin:0 0 8px'

  const saveButton = document.createElement('button')
  saveButton.type = 'button'
  saveButton.setAttribute('data-self-explanation-save', 'true')
  saveButton.textContent = 'Save explanation'
  saveButton.style.cssText = 'padding:6px 12px'

  const hint = document.createElement('p')
  hint.textContent = 'This note is exported under qualitative_notes and never mixed into deterministic_events.'
  hint.style.cssText = 'margin:8px 0 0;font-size:11px;color:#b8c4d1'

  saveButton.addEventListener('click', () => {
    // B1/B6: persist via injected recorder (state-scoped), then re-sync the
    // prompt from the fresh snapshot. No module-global write.
    deps.onSaveExplanation(response.value.trim())
    syncPrompt()
  })

  card.appendChild(title)
  card.appendChild(prompt)
  card.appendChild(response)
  card.appendChild(saveButton)
  card.appendChild(hint)
  container.appendChild(card)

  function syncPrompt(): void {
    const snapshot = deps.getSnapshot()
    if (!snapshot.qualitative_notes) {
      card.hidden = true
      return
    }
    card.hidden = false
    prompt.textContent = snapshot.qualitative_notes.self_explanation_prompt
    if (
      snapshot.qualitative_notes.self_explanation_response !== undefined &&
      response.value !== snapshot.qualitative_notes.self_explanation_response
    ) {
      response.value = snapshot.qualitative_notes.self_explanation_response
    }
  }

  return { card, syncPrompt }
}

/** Build and mount the Evidence Panel DOM node (AC2, AC9, AC10) */
function mountPanel(
  container: HTMLElement,
  initiallyHidden: boolean,
  deps: PlaytestEvidencePanelDeps,
): MountPanelResult {
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

  // Refresh evidence button (B6): explicit, on-demand snapshot recompute.
  const refreshBtn = document.createElement('button')
  refreshBtn.setAttribute('data-action', 'refresh-evidence')
  refreshBtn.type = 'button'
  refreshBtn.textContent = 'Refresh evidence'
  refreshBtn.style.cssText =
    'margin-left:8px;padding:6px 12px;background:#3a3a5e;color:#fff;border:none;cursor:pointer;font-size:12px'
  refreshBtn.addEventListener('click', () => {
    // Force a recompute even when the snapshot is already locked.
    snapshotData = null
    refreshSnapshot()
  })

  const btnRow = document.createElement('div')
  btnRow.style.cssText = 'margin-bottom:12px'
  btnRow.appendChild(copyBtn)
  btnRow.appendChild(downloadBtn)
  btnRow.appendChild(refreshBtn)

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
      refreshSnapshot()
    }
  }).catch(() => {
    // Async update failed silently -- sync render remains
  })

  /**
   * B6: Recompute the atomic evidence snapshot from the current runtime state.
   *
   * Called only on explicit events (panel open, Save explanation, Refresh
   * evidence). The first call locks `generated_at`; subsequent refreshes reuse
   * it so display / copy / download always operate on a single coherent
   * snapshot. There is no per-frame refresh loop.
   */
  function refreshSnapshot(): void {
    const generatedAt = snapshotData?.generated_at
    let data = buildEvidenceData(deps.getSnapshot(), generatedAt)
    // Apply async browser info if already resolved
    if (asyncBrowserCache && asyncBrowserCache.version_source === 'userAgentData') {
      data = { ...data, browser: asyncBrowserCache }
    }
    snapshotData = data
    currentYaml = toYaml(data)
    textarea.value = currentYaml
  }

  /**
   * B6: Initialize the evidence snapshot on first open only. Once locked, a
   * reopen does NOT recompute the snapshot (avoids the previous double-refresh).
   * Use the explicit Refresh evidence button to force a recompute.
   */
  function initSnapshot(): void {
    if (snapshotData !== null) {
      return
    }
    refreshSnapshot()
  }

  return { panel, initSnapshot, refreshSnapshot }
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
 * @param options   - Injected runtime deps (B1) plus optional URL search string.
 *   `getSnapshot` / `onSaveExplanation` bind the panel to a state-scoped runtime.
 *   For backwards-compatible callers/tests, a bare search string may be passed
 *   instead; in that case empty default deps are used.
 */
export interface InitPlaytestEvidencePanelOptions extends Partial<PlaytestEvidencePanelDeps> {
  search?: string
}

function resolvePanelDeps(deps: Partial<PlaytestEvidencePanelDeps>): PlaytestEvidencePanelDeps {
  return {
    getSnapshot: deps.getSnapshot ?? (() => EMPTY_RUNTIME_SNAPSHOT),
    onSaveExplanation: deps.onSaveExplanation ?? (() => {}),
  }
}

export function initPlaytestEvidencePanel(
  container: HTMLElement,
  optionsOrSearch?: string | InitPlaytestEvidencePanelOptions,
): void {
  // AC13: idempotent -- do not mount more than once per container
  if (container.querySelector('[data-playtest-toggle="true"]')) {
    return
  }

  const options: InitPlaytestEvidencePanelOptions =
    typeof optionsOrSearch === 'string' || optionsOrSearch === undefined
      ? { search: optionsOrSearch }
      : optionsOrSearch
  const deps = resolvePanelDeps(options)

  const q = options.search ?? (typeof location !== 'undefined' ? location.search : '')
  const panelOpen = shouldShowPanel(q)

  // Mount panel (initially hidden unless ?playtest_evidence=1)
  const { panel, initSnapshot } = mountPanel(container, !panelOpen, deps)
  const { syncPrompt } = mountSelfExplanationPrompt(container, deps)

  // B6: snapshot is initialized on first open only. No per-frame refresh loop.
  // The prompt card is synced on the same explicit events.
  if (panelOpen) {
    initSnapshot()
  }
  syncPrompt()

  // Wire prompt re-sync to panel open via the toggle (B6).
  mountToggle(container, panel, () => {
    initSnapshot()
    syncPrompt()
  })
}
