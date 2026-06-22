/**
 * EntireCLI safety checker — adapter layer
 *
 * schema_version: entirecli_safety_result/v1
 *
 * Determines whether EntireCLI usage is safe for inclusion in a public run report.
 *
 * DUAL-IMPLEMENTATION NOTE (Blocker 7):
 * This JS module is an independent implementation of the EntireCLI safety logic.
 * The existing Python verifier (.claude/scripts/check_session_recording_runtime_safety.py)
 * performs overlapping checks (hook files, visibility, redaction, fail-closed). Rather than
 * call the Python verifier as a subprocess (which would add a Python runtime dependency to the
 * JS agent-logs pipeline and is outside the Allowed Paths of this PR), this JS module
 * reimplements the required checks independently. The relationship and dual-implementation
 * status are documented here for future maintainers. Any changes to safety logic must be
 * applied to both implementations. See docs/dev/agent-run-report.md for the canonical policy.
 *
 * Verdict rules:
 *   not_applicable — no EntireCLI presence detected (binary, dirs, refs, env, config)
 *   safe           — EntireCLI present AND all of: no push sessions, telemetry=false,
 *                    private/local checkpoint remote, token+private checks pass
 *   blocked        — any unsafe condition: public branch, push enabled, telemetry enabled/unknown,
 *                    unknown visibility, parse error, auth/network error, raw value emission
 *
 * Raw values (tokens, URLs, absolute paths) are NEVER emitted; only reason_codes and
 * redacted fingerprints appear in output.
 */

export const SCHEMA_VERSION = 'entirecli_safety_result/v1'

/**
 * Reason codes emitted in the result.
 * @readonly
 * @enum {string}
 */
export const ReasonCode = /** @type {const} */ ({
  ENTIRE_ABSENT: 'entire_absent',
  PUBLIC_CHECKPOINT_BRANCH_PRESENT: 'public_checkpoint_branch_present',
  PUSH_SESSIONS_ENABLED: 'push_sessions_enabled',
  PUSH_SESSIONS_UNKNOWN: 'push_sessions_unknown',
  CHECKPOINT_REMOTE_UNKNOWN_VISIBILITY: 'checkpoint_remote_unknown_visibility',
  CHECKPOINT_REMOTE_PUBLIC: 'checkpoint_remote_public',
  CHECKPOINT_TOKEN_WITHOUT_PRIVATE_REMOTE: 'checkpoint_token_without_private_remote',
  PUBLIC_PUSH_REMOTE_DETECTED: 'public_push_remote_detected',
  GIT_CONFIG_PARSE_ERROR: 'git_config_parse_error',
  TELEMETRY_ENABLED: 'telemetry_enabled',
  TELEMETRY_UNKNOWN: 'telemetry_unknown',
  RAW_VALUE_REDACTION_VIOLATION: 'raw_value_redaction_violation',
  SETTINGS_PARSE_ERROR: 'settings_parse_error',
  CODE_REMOTE_UNKNOWN_VISIBILITY: 'code_remote_unknown_visibility',
})

/**
 * Remote branches patterns that must be inspected for public exposure.
 * @type {RegExp[]}
 */
const CHECKPOINT_BRANCH_PATTERNS = [
  /^entire\/checkpoints\/v1/,
  /^entire\//,
  /checkpoint/i,
  /session/i,
]

/**
 * Git config keys to scan for push/remote paths.
 * @type {string[]}
 */
export const GIT_CONFIG_KEYS = [
  'remote.*.url',
  'remote.*.pushurl',
  'remote.pushDefault',
  'branch.*.pushRemote',
  'url.*.insteadOf',
  'url.*.pushInsteadOf',
  'include.path',
  'includeIf.*.path',
  'remote.*.mirror',
  'remote.*.push',
]

/**
 * Patterns for raw value detection (must not appear in diagnostic output).
 * @type {RegExp[]}
 */
const RAW_VALUE_PATTERNS = [
  /\bghp_[A-Za-z0-9]{8,}\b/,
  /\bgithub_pat_[A-Za-z0-9_]{8,}\b/,
  /\bsk-[A-Za-z0-9]{8,}\b/,
  /\bsk-proj-[A-Za-z0-9_-]{8,}\b/,
  /\bAKIA[A-Z0-9]{16}\b/,
  /(^|[^A-Za-z0-9._-])\/home\/[^\s"'`]+/,
  /(^|[^A-Za-z0-9._-])\/Users\/[^\s"'`]+/,
]

/**
 * GitHub remote URL pattern.
 * @type {RegExp}
 */
const GITHUB_URL_PATTERN = /github\.com[:/]/i

/**
 * Sentinel object to distinguish parse error from empty settings.
 * @type {{ parse_error: true }}
 */
export const SETTINGS_PARSE_ERROR_SENTINEL = { parse_error: /** @type {true} */ (true) }

/**
 * Redact a fingerprint value: keep first 4 chars and hash-length indicator.
 * @param {string} value
 * @returns {string}
 */
export function redactFingerprint(value) {
  if (!value || value.length === 0) return '[empty]'
  const prefix = value.slice(0, 4).replace(/[^A-Za-z0-9]/g, '*')
  return `${prefix}***[len=${value.length}]`
}

/**
 * Check if a string contains raw sensitive values.
 * @param {string} text
 * @returns {boolean}
 */
export function containsRawValue(text) {
  return RAW_VALUE_PATTERNS.some((pattern) => pattern.test(text))
}

/**
 * Deep merge strategy_options from base and local settings.
 * Local settings override base settings at the field level within strategy_options.
 *
 * Handles both top-level `telemetry` (official schema) and
 * `strategy_options.telemetry` (legacy/alternate schema).
 *
 * @param {Record<string, unknown>} baseSettings
 * @param {Record<string, unknown>} localSettings
 * @returns {{ pushSessions: boolean | null, telemetry: boolean | null, checkpointRemoteObj: { provider?: string, repo?: string } | string | null }}
 */
export function parseEntireSettings(baseSettings, localSettings) {
  // Deep merge strategy_options
  const baseStrategyOptions = /** @type {Record<string, unknown>} */ (baseSettings.strategy_options ?? {})
  const localStrategyOptions = /** @type {Record<string, unknown>} */ (localSettings.strategy_options ?? {})
  const mergedStrategyOptions = { ...baseStrategyOptions, ...localStrategyOptions }

  // Merge top-level fields (local overrides base)
  const mergedTopLevel = { ...baseSettings, ...localSettings }

  const pushSessions =
    typeof mergedStrategyOptions.push_sessions === 'boolean'
      ? mergedStrategyOptions.push_sessions
      : null

  // telemetry: check top-level first (official schema), then strategy_options (alternate)
  const rawTelemetry =
    typeof mergedTopLevel.telemetry === 'boolean'
      ? mergedTopLevel.telemetry
      : typeof mergedStrategyOptions.telemetry === 'boolean'
        ? mergedStrategyOptions.telemetry
        : null

  const telemetry = rawTelemetry

  // checkpoint_remote: official schema is strategy_options.checkpoint_remote as { provider, repo } object or string
  // Also check top-level (legacy)
  const rawCheckpointRemote =
    mergedStrategyOptions.checkpoint_remote !== undefined
      ? mergedStrategyOptions.checkpoint_remote
      : mergedTopLevel.checkpoint_remote !== undefined
        ? mergedTopLevel.checkpoint_remote
        : null

  // Normalize to object or string or null
  const checkpointRemoteObj =
    rawCheckpointRemote !== null && rawCheckpointRemote !== undefined
      ? (typeof rawCheckpointRemote === 'object'
          ? /** @type {{ provider?: string, repo?: string }} */ (rawCheckpointRemote)
          : typeof rawCheckpointRemote === 'string'
            ? rawCheckpointRemote
            : null)
      : null

  return { pushSessions, telemetry, checkpointRemoteObj }
}

/**
 * Determine remote visibility verdict.
 * @param {'private' | 'public' | 'unknown' | 'not_github' | 'local_only'} visibility
 * @returns {'private_verified' | 'local_only' | 'blocked'}
 */
export function classifyRemoteVisibility(visibility) {
  if (visibility === 'private') return 'private_verified'
  if (visibility === 'local_only') return 'local_only'
  return 'blocked'
}

/**
 * Check whether a branch name matches any known checkpoint/session pattern.
 * @param {string} branchName
 * @returns {boolean}
 */
export function isCheckpointBranch(branchName) {
  return CHECKPOINT_BRANCH_PATTERNS.some((pattern) => pattern.test(branchName))
}

/**
 * Main EntireCLI safety check function.
 *
 * @param {object} opts
 * @param {boolean} opts.entireBinaryPresent - whether `entire` binary is on PATH
 * @param {string | null} [opts.entireVersion] - `entire version` output or null
 * @param {string | null} [opts.entireEnableHelp] - `entire enable --help` surface or null
 * @param {string | null} [opts.entireConfigureHelp] - `entire configure --help` surface or null
 * @param {boolean} opts.entireDirPresent - whether `.entire/` directory exists
 * @param {boolean} opts.entireHooksPresent - whether Entire hooks are installed
 * @param {string[]} opts.localRefs - local git refs (to detect entire/checkpoints/v1)
 * @param {boolean} opts.checkpointTrailerPresent - whether commit trailers include entire checkpoints
 * @param {boolean} opts.tokenEnvPresent - whether ENTIRE_CHECKPOINT_TOKEN env var is set
 * @param {Record<string, unknown> | { parse_error: true }} opts.baseSettings - parsed .entire/settings.json or parse error sentinel
 * @param {Record<string, unknown> | { parse_error: true }} opts.localSettings - parsed .entire/settings.local.json (override) or parse error sentinel
 * @param {string | null} opts.checkpointRemote - checkpoint_remote config value (string name) or null
 * @param {'private' | 'public' | 'unknown' | 'not_github' | 'local_only'} opts.checkpointRemoteVisibility
 * @param {'private' | 'public' | 'unknown' | 'not_github' | 'local_only'} [opts.codeRemoteVisibility] - visibility of code remote (e.g. origin) when checkpointRemote not set
 * @param {string[]} opts.remoteBranches - remote branch names to inspect
 * @param {Record<string, string[]>} opts.gitConfig - multi-value git config key→values[]
 * @param {string[]} opts.gitConfigParseErrors - any parse error messages
 * @param {string[]} opts.diagnosticStrings - all strings that will appear in output (for redaction check)
 * @returns {EntireCLISafetyResult}
 */
export function checkEntireCLISafety(opts) {
  const {
    entireBinaryPresent,
    entireVersion = null,
    entireEnableHelp = null,
    entireConfigureHelp = null,
    entireDirPresent,
    entireHooksPresent,
    localRefs,
    checkpointTrailerPresent,
    tokenEnvPresent,
    baseSettings,
    localSettings,
    checkpointRemote,
    checkpointRemoteVisibility,
    codeRemoteVisibility = 'unknown',
    remoteBranches,
    gitConfig,
    gitConfigParseErrors,
    diagnosticStrings,
  } = opts

  const reasonCodes = /** @type {string[]} */ ([])

  // --- Redaction guard: check all diagnostic strings before proceeding ---
  const allDiagnostics = diagnosticStrings.join('\n')
  if (containsRawValue(allDiagnostics)) {
    return {
      schema_version: SCHEMA_VERSION,
      verdict: 'blocked',
      reason_codes: [ReasonCode.RAW_VALUE_REDACTION_VIOLATION],
      raw_values_emitted: true,
      checked_surfaces: buildCheckedSurfaces(entireBinaryPresent, entireVersion, entireEnableHelp, entireConfigureHelp),
    }
  }

  // --- Detect EntireCLI presence ---
  const hasEntireRef = localRefs.some((ref) => isCheckpointBranch(ref))

  // Settings parse error counts as presence detection
  const baseHasParseError = 'parse_error' in baseSettings && baseSettings.parse_error === true
  const localHasParseError = 'parse_error' in localSettings && localSettings.parse_error === true

  const safeBase = baseHasParseError ? {} : /** @type {Record<string, unknown>} */ (baseSettings)
  const safeLocal = localHasParseError ? {} : /** @type {Record<string, unknown>} */ (localSettings)

  const entirePresent =
    entireBinaryPresent ||
    entireDirPresent ||
    entireHooksPresent ||
    hasEntireRef ||
    checkpointTrailerPresent ||
    tokenEnvPresent ||
    Object.keys(safeBase).length > 0 ||
    Object.keys(safeLocal).length > 0 ||
    baseHasParseError ||
    localHasParseError ||
    checkpointRemote !== null

  if (!entirePresent) {
    return {
      schema_version: SCHEMA_VERSION,
      verdict: 'not_applicable',
      reason_codes: [ReasonCode.ENTIRE_ABSENT],
      raw_values_emitted: false,
      checked_surfaces: buildCheckedSurfaces(entireBinaryPresent, entireVersion, entireEnableHelp, entireConfigureHelp),
    }
  }

  // --- Settings parse error → blocked ---
  if (baseHasParseError || localHasParseError) {
    reasonCodes.push(ReasonCode.SETTINGS_PARSE_ERROR)
  }

  // --- EntireCLI is present; evaluate safety conditions ---

  // Parse settings (local overrides base, deep merge strategy_options)
  const { pushSessions, telemetry } = parseEntireSettings(safeBase, safeLocal)

  // Check push_sessions
  if (pushSessions === true) {
    reasonCodes.push(ReasonCode.PUSH_SESSIONS_ENABLED)
  } else if (pushSessions === null && !baseHasParseError && !localHasParseError) {
    reasonCodes.push(ReasonCode.PUSH_SESSIONS_UNKNOWN)
  }

  // Check telemetry (undefined → blocked)
  if (telemetry === true) {
    reasonCodes.push(ReasonCode.TELEMETRY_ENABLED)
  } else if (telemetry === null && !baseHasParseError && !localHasParseError) {
    reasonCodes.push(ReasonCode.TELEMETRY_UNKNOWN)
  }

  // Check checkpoint_remote visibility
  if (checkpointRemote !== null) {
    const visibilityClass = classifyRemoteVisibility(checkpointRemoteVisibility)
    if (visibilityClass === 'blocked') {
      if (checkpointRemoteVisibility === 'public') {
        reasonCodes.push(ReasonCode.CHECKPOINT_REMOTE_PUBLIC)
      } else {
        reasonCodes.push(ReasonCode.CHECKPOINT_REMOTE_UNKNOWN_VISIBILITY)
      }
    }
    // Token present without private remote → blocked
    if (tokenEnvPresent && visibilityClass !== 'private_verified') {
      reasonCodes.push(ReasonCode.CHECKPOINT_TOKEN_WITHOUT_PRIVATE_REMOTE)
    }
  } else {
    // Blocker 1: checkpoint_remote not configured → Entire will use code remote (origin etc.)
    // We must check the code remote visibility. If unknown/public → blocked.
    if (tokenEnvPresent) {
      // Token present but no checkpoint_remote configured → blocked
      reasonCodes.push(ReasonCode.CHECKPOINT_TOKEN_WITHOUT_PRIVATE_REMOTE)
    }
    // Check code remote visibility: if public or unknown → Entire may push checkpoints there
    const codeVisibilityClass = classifyRemoteVisibility(codeRemoteVisibility)
    if (codeVisibilityClass === 'blocked') {
      if (codeRemoteVisibility === 'public') {
        reasonCodes.push(ReasonCode.CODE_REMOTE_UNKNOWN_VISIBILITY)
      } else {
        // unknown, not_github → fail-closed
        reasonCodes.push(ReasonCode.CODE_REMOTE_UNKNOWN_VISIBILITY)
      }
    }
  }

  // Check remote branches for public checkpoint/session refs
  for (const branch of remoteBranches) {
    if (isCheckpointBranch(branch)) {
      reasonCodes.push(ReasonCode.PUBLIC_CHECKPOINT_BRANCH_PRESENT)
      break
    }
  }

  // Check git config for push remotes that are public
  if (gitConfigParseErrors.length > 0) {
    reasonCodes.push(ReasonCode.GIT_CONFIG_PARSE_ERROR)
  } else {
    // Inspect push-related config values for public remote indicators
    // gitConfig is now Record<string, string[]> (multi-value)
    let foundPublicPush = false

    /**
     * @param {string} value
     */
    const checkPushUrl = (value) => {
      if (!foundPublicPush && value) {
        if (!GITHUB_URL_PATTERN.test(value) && value.startsWith('http')) {
          reasonCodes.push(ReasonCode.PUBLIC_PUSH_REMOTE_DETECTED)
          foundPublicPush = true
        }
      }
    }

    for (const [key, values] of Object.entries(gitConfig)) {
      if (foundPublicPush) break
      const isPushUrlKey =
        key.includes('pushurl') ||
        key.includes('pushinsteadof') ||
        key.includes('pushremote') ||
        key.includes('pushdefault') ||
        (key.includes('remote.') && key.endsWith('.push'))

      if (isPushUrlKey) {
        // values is string[] for multi-value support
        const valArr = Array.isArray(values) ? values : [values]
        for (const v of valArr) {
          checkPushUrl(v)
        }
      }

      // For mirror=true, check the associated remote URL
      if (key.includes('remote.') && key.endsWith('.mirror')) {
        const valArr = Array.isArray(values) ? values : [values]
        if (valArr.includes('true')) {
          // Extract remote name: remote.<name>.mirror → look up remote.<name>.url
          const parts = key.split('.')
          if (parts.length >= 3) {
            const remoteName = parts.slice(1, parts.length - 1).join('.')
            const remoteUrls = gitConfig[`remote.${remoteName}.url`] ?? []
            const urlArr = Array.isArray(remoteUrls) ? remoteUrls : [remoteUrls]
            for (const remoteUrl of urlArr) {
              if (remoteUrl && !GITHUB_URL_PATTERN.test(remoteUrl) && remoteUrl.startsWith('http')) {
                reasonCodes.push(ReasonCode.PUBLIC_PUSH_REMOTE_DETECTED)
                foundPublicPush = true
                break
              }
            }
          }
        }
      }
    }
  }

  const isBlocked = reasonCodes.length > 0

  return {
    schema_version: SCHEMA_VERSION,
    verdict: isBlocked ? 'blocked' : 'safe',
    reason_codes: isBlocked ? [...new Set(reasonCodes)] : [],
    raw_values_emitted: false,
    checked_surfaces: buildCheckedSurfaces(entireBinaryPresent, entireVersion, entireEnableHelp, entireConfigureHelp),
  }
}

/**
 * Build checked_surfaces object for AC6.
 * Records binary presence and surface availability without raw output.
 *
 * @param {boolean} entireBinaryPresent
 * @param {string | null} entireVersion
 * @param {string | null} entireEnableHelp
 * @param {string | null} entireConfigureHelp
 * @returns {CheckedSurfaces}
 */
function buildCheckedSurfaces(entireBinaryPresent, entireVersion, entireEnableHelp, entireConfigureHelp) {
  return {
    entire_binary: entireBinaryPresent,
    entire_version: entireVersion !== null ? redactFingerprint(entireVersion) : null,
    entire_enable_help: entireEnableHelp !== null,
    entire_configure_help: entireConfigureHelp !== null,
  }
}

/**
 * @typedef {object} CheckedSurfaces
 * @property {boolean} entire_binary - whether `entire` binary was found on PATH
 * @property {string | null} entire_version - redacted version fingerprint or null if unavailable
 * @property {boolean} entire_enable_help - whether `entire enable --help` surface is available
 * @property {boolean} entire_configure_help - whether `entire configure --help` surface is available
 */

/**
 * @typedef {object} EntireCLISafetyResult
 * @property {string} schema_version
 * @property {'not_applicable' | 'safe' | 'blocked'} verdict
 * @property {string[]} reason_codes
 * @property {boolean} raw_values_emitted
 * @property {CheckedSurfaces} checked_surfaces
 */
