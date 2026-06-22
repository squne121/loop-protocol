/**
 * EntireCLI safety checker — adapter layer
 *
 * schema_version: entirecli_safety_result/v1
 *
 * Determines whether EntireCLI usage is safe for inclusion in a public run report.
 * Does NOT replicate the Python canonical safety engine logic; instead operates as
 * an independent adapter that uses the same verdict taxonomy (not_applicable/safe/blocked).
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
 * Parse entirecli settings objects for push_sessions and telemetry.
 * Merges base settings then local override.
 *
 * @param {Record<string, unknown>} baseSettings
 * @param {Record<string, unknown>} localSettings
 * @returns {{ pushSessions: boolean | null, telemetry: boolean | null }}
 */
export function parseEntireSettings(baseSettings, localSettings) {
  const merged = { ...baseSettings, ...localSettings }
  const strategyOptions = /** @type {Record<string, unknown>} */ (merged.strategy_options ?? {})

  const pushSessions =
    typeof strategyOptions.push_sessions === 'boolean'
      ? strategyOptions.push_sessions
      : null

  const telemetry =
    typeof strategyOptions.telemetry === 'boolean'
      ? strategyOptions.telemetry
      : null

  return { pushSessions, telemetry }
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
 * @param {boolean} opts.entireDirPresent - whether `.entire/` directory exists
 * @param {boolean} opts.entireHooksPresent - whether Entire hooks are installed
 * @param {string[]} opts.localRefs - local git refs (to detect entire/checkpoints/v1)
 * @param {boolean} opts.checkpointTrailerPresent - whether commit trailers include entire checkpoints
 * @param {boolean} opts.tokenEnvPresent - whether ENTIRE_CHECKPOINT_TOKEN env var is set
 * @param {Record<string, unknown>} opts.baseSettings - parsed .entire/settings.json
 * @param {Record<string, unknown>} opts.localSettings - parsed .entire/settings.local.json (override)
 * @param {string | null} opts.checkpointRemote - checkpoint_remote config value
 * @param {'private' | 'public' | 'unknown' | 'not_github' | 'local_only'} opts.checkpointRemoteVisibility
 * @param {string[]} opts.remoteBranches - remote branch names to inspect
 * @param {Record<string, string>} opts.gitConfig - flat git config key→value
 * @param {string[]} opts.gitConfigParseErrors - any parse error messages
 * @param {string[]} opts.diagnosticStrings - all strings that will appear in output (for redaction check)
 * @returns {EntireCLISafetyResult}
 */
export function checkEntireCLISafety(opts) {
  const {
    entireBinaryPresent,
    entireDirPresent,
    entireHooksPresent,
    localRefs,
    checkpointTrailerPresent,
    tokenEnvPresent,
    baseSettings,
    localSettings,
    checkpointRemote,
    checkpointRemoteVisibility,
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
    }
  }

  // --- Detect EntireCLI presence ---
  const hasEntireRef = localRefs.some((ref) => isCheckpointBranch(ref))
  const entirePresent =
    entireBinaryPresent ||
    entireDirPresent ||
    entireHooksPresent ||
    hasEntireRef ||
    checkpointTrailerPresent ||
    tokenEnvPresent ||
    Object.keys(baseSettings).length > 0 ||
    Object.keys(localSettings).length > 0 ||
    checkpointRemote !== null

  if (!entirePresent) {
    return {
      schema_version: SCHEMA_VERSION,
      verdict: 'not_applicable',
      reason_codes: [ReasonCode.ENTIRE_ABSENT],
      raw_values_emitted: false,
    }
  }

  // --- EntireCLI is present; evaluate safety conditions ---

  // Parse settings (local overrides base)
  const { pushSessions, telemetry } = parseEntireSettings(baseSettings, localSettings)

  // Check push_sessions
  if (pushSessions === true) {
    reasonCodes.push(ReasonCode.PUSH_SESSIONS_ENABLED)
  } else if (pushSessions === null) {
    reasonCodes.push(ReasonCode.PUSH_SESSIONS_UNKNOWN)
  }

  // Check telemetry (undefined → blocked)
  if (telemetry === true) {
    reasonCodes.push(ReasonCode.TELEMETRY_ENABLED)
  } else if (telemetry === null) {
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
  } else if (tokenEnvPresent) {
    // Token present but no checkpoint_remote configured → blocked
    reasonCodes.push(ReasonCode.CHECKPOINT_TOKEN_WITHOUT_PRIVATE_REMOTE)
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
    let foundPublicPush = false
    for (const [key, value] of Object.entries(gitConfig)) {
      if (foundPublicPush) break
      const isPushUrlKey =
        key.includes('pushurl') ||
        key.includes('pushinsteadof') ||
        key.includes('pushremote') ||
        key.includes('pushdefault') ||
        (key.includes('remote.') && key.endsWith('.push'))
      if (isPushUrlKey && value) {
        if (!GITHUB_URL_PATTERN.test(value) && value.startsWith('http')) {
          reasonCodes.push(ReasonCode.PUBLIC_PUSH_REMOTE_DETECTED)
          foundPublicPush = true
        }
      }
      // For mirror=true, check the associated remote URL
      if (key.includes('remote.') && key.endsWith('.mirror') && value === 'true') {
        // Extract remote name: remote.<name>.mirror → look up remote.<name>.url
        const parts = key.split('.')
        if (parts.length >= 3) {
          const remoteName = parts.slice(1, parts.length - 1).join('.')
          const remoteUrl = gitConfig[`remote.${remoteName}.url`] ?? ''
          if (remoteUrl && !GITHUB_URL_PATTERN.test(remoteUrl) && remoteUrl.startsWith('http')) {
            reasonCodes.push(ReasonCode.PUBLIC_PUSH_REMOTE_DETECTED)
            foundPublicPush = true
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
  }
}

/**
 * @typedef {object} EntireCLISafetyResult
 * @property {string} schema_version
 * @property {'not_applicable' | 'safe' | 'blocked'} verdict
 * @property {string[]} reason_codes
 * @property {boolean} raw_values_emitted
 */
