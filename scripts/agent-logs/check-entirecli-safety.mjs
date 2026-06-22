#!/usr/bin/env node

/**
 * EntireCLI safety checker CLI
 *
 * Checks whether EntireCLI usage is safe for public run reports.
 * Outputs JSON verdict to stdout or writes to --output file.
 *
 * Usage:
 *   node scripts/agent-logs/check-entirecli-safety.mjs [--output <path>]
 *
 * Environment:
 *   ENTIRE_CHECKPOINT_TOKEN — if set, triggers token presence check
 */

import { existsSync, readFileSync } from 'fs'
import { resolve } from 'path'
import { execFileSync } from 'child_process'

import {
  checkEntireCLISafety,
  redactFingerprint,
  SETTINGS_PARSE_ERROR_SENTINEL,
} from './lib/entirecli-safety.mjs'
import { parseArgs, printCliError } from './lib/args.mjs'
import { writeJsonAtomic } from './lib/atomic-json.mjs'

const OPTION_SPEC = {
  '--output': { key: 'outputPath' },
  '--repo-root': { key: 'repoRoot', defaultValue: process.cwd() },
}

/**
 * Run a command and return stdout or null on failure.
 * @param {string} cmd
 * @param {string[]} args
 * @param {string} cwd
 * @returns {string | null}
 */
function runCommand(cmd, args, cwd) {
  try {
    return execFileSync(cmd, args, {
      cwd,
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
    }).trim()
  } catch {
    return null
  }
}

/**
 * Check if entire binary is available and collect surface info.
 * Returns { present, version, enableHelp, configureHelp }.
 *
 * @param {string} cwd
 * @returns {{ present: boolean, version: string | null, enableHelp: string | null, configureHelp: string | null }}
 */
function detectEntireBinary(cwd) {
  const version = runCommand('entire', ['version'], cwd)
  const present = version !== null
  if (!present) {
    return { present: false, version: null, enableHelp: null, configureHelp: null }
  }
  // Collect help surfaces — raw output is NOT emitted, only presence flag
  const enableHelp = runCommand('entire', ['enable', '--help'], cwd)
  const configureHelp = runCommand('entire', ['configure', '--help'], cwd)
  return { present: true, version, enableHelp, configureHelp }
}

/**
 * List remote branches from git.
 * @param {string} cwd
 * @returns {string[]}
 */
function listRemoteBranches(cwd) {
  const output = runCommand('git', ['branch', '-r', '--format=%(refname:short)'], cwd)
  if (!output) return []
  return output
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
}

/**
 * List local git refs.
 * @param {string} cwd
 * @returns {string[]}
 */
function listLocalRefs(cwd) {
  const output = runCommand('git', ['for-each-ref', '--format=%(refname)', 'refs/'], cwd)
  if (!output) return []
  return output
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
}

/**
 * Get multi-value git config as key→string[] map using NUL-separated output.
 *
 * Uses `git config -z --list` which outputs NUL-separated key\nvalue pairs,
 * correctly handling multi-value keys (remote.*.pushurl etc.) and special chars.
 *
 * Also collects:
 * - pushurl fallback (pushurl absent → url is push destination)
 * - pushInsteadOf direction: value is prefix → key is replacement
 * - include.path / includeIf.*.path are recorded (content not read)
 *
 * @param {string} cwd
 * @returns {{ config: Record<string, string[]>, errors: string[] }}
 */
function loadGitConfig(cwd) {
  // Use -z for NUL-terminated output: each entry is "key\nvalue\0"
  const raw = runCommand('git', ['config', '-z', '--list'], cwd)
  if (raw === null) {
    return { config: {}, errors: ['git config -z --list failed'] }
  }

  const config = /** @type {Record<string, string[]>} */ ({})
  const errors = /** @type {string[]} */ ([])

  // Split on NUL; each chunk is "key\nvalue" (key may not contain \n, value may)
  const entries = raw.split('\0').filter((e) => e.length > 0)
  for (const entry of entries) {
    const nlIndex = entry.indexOf('\n')
    if (nlIndex < 0) {
      errors.push(`malformed git config entry: ${entry.length} chars`)
      continue
    }
    const key = entry.slice(0, nlIndex).trim().toLowerCase()
    const value = entry.slice(nlIndex + 1)
    if (!config[key]) {
      config[key] = []
    }
    config[key].push(value)
  }

  return { config, errors }
}

/**
 * Try to parse a JSON file.
 * Returns SETTINGS_PARSE_ERROR_SENTINEL on parse error or read error.
 * Returns empty object {} if file does not exist.
 *
 * @param {string} filePath
 * @returns {Record<string, unknown> | { parse_error: true }}
 */
function tryParseJsonFile(filePath) {
  if (!existsSync(filePath)) {
    return {}
  }
  try {
    const raw = readFileSync(filePath, 'utf-8')
    return JSON.parse(raw)
  } catch {
    // Distinguish parse error from not-found: file exists but is malformed → blocked
    return SETTINGS_PARSE_ERROR_SENTINEL
  }
}

/**
 * Check if commit trailers contain entire checkpoint references.
 * @param {string} cwd
 * @returns {boolean}
 */
function detectCheckpointTrailers(cwd) {
  const output = runCommand('git', ['log', '--format=%B', '-10'], cwd)
  if (!output) return false
  return /entire[- ]checkpoint/i.test(output)
}

/**
 * Determine checkpoint remote visibility using GitHub API via `gh` CLI.
 *
 * For GitHub URLs: queries `gh repo view <owner>/<repo> --json isPrivate`
 *   - private: true → 'private'
 *   - private: false → 'public'
 *   - CLI unavailable / auth failure / network error → 'unknown'
 *
 * For local paths: 'local_only'
 * For non-GitHub URLs: 'not_github'
 * For null/empty: 'unknown'
 *
 * @param {string | null} remoteUrl
 * @returns {'private' | 'public' | 'unknown' | 'not_github' | 'local_only'}
 */
function detectCheckpointRemoteVisibility(remoteUrl) {
  if (!remoteUrl) return 'unknown'
  if (remoteUrl.startsWith('/') || remoteUrl.startsWith('./') || remoteUrl.startsWith('file://')) {
    return 'local_only'
  }
  if (!/github\.com/i.test(remoteUrl)) {
    return 'not_github'
  }

  // Extract owner/repo from GitHub URL
  // Handles: https://github.com/owner/repo.git, git@github.com:owner/repo.git
  const match = remoteUrl.match(/github\.com[:/]([^/]+\/[^/]+?)(?:\.git)?$/)
  if (!match) return 'unknown'

  const ownerRepo = match[1]

  try {
    const output = execFileSync('gh', ['repo', 'view', ownerRepo, '--json', 'isPrivate'], {
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
      timeout: 10000,
    }).trim()

    const parsed = JSON.parse(output)
    if (typeof parsed.isPrivate === 'boolean') {
      return parsed.isPrivate ? 'private' : 'public'
    }
    return 'unknown'
  } catch {
    // gh unavailable, auth failure, network error → unknown → fail-closed
    return 'unknown'
  }
}

/**
 * Resolve checkpoint remote URL from git config multi-value map.
 * Handles: remote.*.pushurl (if present), fallback to remote.*.url
 * Also applies url.*.pushInsteadOf rewrites.
 *
 * @param {string} remoteName
 * @param {Record<string, string[]>} config
 * @returns {string | null}
 */
function resolveRemotePushUrl(remoteName, config) {
  // pushurl takes precedence over url for push operations
  const pushUrls = config[`remote.${remoteName}.pushurl`]
  if (pushUrls && pushUrls.length > 0) {
    return pushUrls[0] // Use first pushurl
  }
  const urls = config[`remote.${remoteName}.url`]
  if (urls && urls.length > 0) {
    let url = urls[0]
    // Apply pushInsteadOf rewrites: url.*.pushInsteadOf: if value is prefix of url → replace with key
    for (const [cfgKey, cfgValues] of Object.entries(config)) {
      if (cfgKey.startsWith('url.') && cfgKey.endsWith('.pushinsteadof')) {
        const replacement = cfgKey.slice(4, cfgKey.length - '.pushinsteadof'.length)
        for (const prefix of cfgValues) {
          if (url.startsWith(prefix)) {
            url = replacement + url.slice(prefix.length)
            break
          }
        }
      }
    }
    return url
  }
  return null
}

async function main() {
  const options = parseArgs(process.argv.slice(2), OPTION_SPEC)
  const repoRoot = resolve(options.repoRoot)

  // Collect surface info
  const {
    present: entireBinaryPresent,
    version: entireVersion,
    enableHelp: entireEnableHelp,
    configureHelp: entireConfigureHelp,
  } = detectEntireBinary(repoRoot)

  const entireDirPresent = existsSync(resolve(repoRoot, '.entire'))
  const entireHooksPresent = existsSync(resolve(repoRoot, '.entire', 'hooks'))
  const localRefs = listLocalRefs(repoRoot)
  const remoteBranches = listRemoteBranches(repoRoot)
  const checkpointTrailerPresent = detectCheckpointTrailers(repoRoot)
  const tokenEnvPresent = typeof process.env.ENTIRE_CHECKPOINT_TOKEN === 'string'

  // Settings parse: sentinel on error (Blocker 5)
  const baseSettings = tryParseJsonFile(resolve(repoRoot, '.entire', 'settings.json'))
  const localSettings = tryParseJsonFile(resolve(repoRoot, '.entire', 'settings.local.json'))

  const { config: gitConfig, errors: gitConfigParseErrors } = loadGitConfig(repoRoot)

  // Determine checkpoint_remote from settings (Blocker 2: read strategy_options.checkpoint_remote)
  const safeBase = ('parse_error' in baseSettings && baseSettings.parse_error) ? {} : /** @type {Record<string, unknown>} */ (baseSettings)
  const safeLocal = ('parse_error' in localSettings && localSettings.parse_error) ? {} : /** @type {Record<string, unknown>} */ (localSettings)

  // Deep merge strategy_options to find checkpoint_remote
  const baseStrategyOptions = /** @type {Record<string, unknown>} */ (safeBase.strategy_options ?? {})
  const localStrategyOptions = /** @type {Record<string, unknown>} */ (safeLocal.strategy_options ?? {})
  const mergedStrategyOptions = { ...baseStrategyOptions, ...localStrategyOptions }
  const mergedTopLevel = { ...safeBase, ...safeLocal }

  // checkpoint_remote: official schema is strategy_options.checkpoint_remote as { provider, repo } or string
  const rawCheckpointRemote =
    mergedStrategyOptions.checkpoint_remote !== undefined
      ? mergedStrategyOptions.checkpoint_remote
      : mergedTopLevel.checkpoint_remote !== undefined
        ? mergedTopLevel.checkpoint_remote
        : null

  // Extract remote name string for URL lookup
  let checkpointRemoteStr = null
  if (rawCheckpointRemote !== null && rawCheckpointRemote !== undefined) {
    if (typeof rawCheckpointRemote === 'string') {
      checkpointRemoteStr = rawCheckpointRemote
    } else if (typeof rawCheckpointRemote === 'object' && rawCheckpointRemote !== null) {
      // { provider, repo } → use provider as remote name or construct URL
      const obj = /** @type {{ provider?: string, repo?: string }} */ (rawCheckpointRemote)
      checkpointRemoteStr = obj.provider ?? null
    }
  }

  // Get checkpoint remote URL from git config (Blocker 3: multi-value aware)
  const checkpointRemoteUrl = checkpointRemoteStr
    ? resolveRemotePushUrl(checkpointRemoteStr, gitConfig)
    : null

  // Determine checkpoint remote visibility (Blocker 4: use gh CLI for GitHub)
  const checkpointRemoteVisibility = detectCheckpointRemoteVisibility(checkpointRemoteUrl)

  // Blocker 1: When checkpoint_remote not set, check code remote visibility
  // Entire will use code remote (origin/etc.) for checkpoints if checkpoint_remote unset
  let codeRemoteVisibility = /** @type {'private' | 'public' | 'unknown' | 'not_github' | 'local_only'} */ ('local_only')
  if (checkpointRemoteStr === null) {
    // Determine the effective push remote (pushDefault or 'origin' fallback)
    const pushDefaultValues = gitConfig['remote.pushdefault']
    const pushDefaultName = (pushDefaultValues && pushDefaultValues.length > 0)
      ? pushDefaultValues[0]
      : 'origin'
    const codeRemoteUrl = resolveRemotePushUrl(pushDefaultName, gitConfig)
    codeRemoteVisibility = detectCheckpointRemoteVisibility(codeRemoteUrl)
  }

  // Build redacted diagnostics only — no raw values
  const diagnosticStrings = [
    checkpointRemoteStr ? `checkpoint_remote: ${redactFingerprint(checkpointRemoteStr)}` : '',
    checkpointRemoteUrl ? `remote_url_fingerprint: ${redactFingerprint(checkpointRemoteUrl)}` : '',
    ...gitConfigParseErrors,
  ].filter(Boolean)

  const result = checkEntireCLISafety({
    entireBinaryPresent,
    entireVersion,
    entireEnableHelp,
    entireConfigureHelp,
    entireDirPresent,
    entireHooksPresent,
    localRefs,
    checkpointTrailerPresent,
    tokenEnvPresent,
    baseSettings,
    localSettings,
    checkpointRemote: checkpointRemoteStr,
    checkpointRemoteVisibility,
    codeRemoteVisibility,
    remoteBranches,
    gitConfig,
    gitConfigParseErrors,
    diagnosticStrings,
  })

  if (options.outputPath) {
    await writeJsonAtomic(options.outputPath, result)
    console.log(`check-entirecli-safety: verdict=${result.verdict} written to ${options.outputPath}`)
  } else {
    console.log(JSON.stringify(result, null, 2))
  }
}

main().catch((error) => {
  process.exit(printCliError('check-entirecli-safety', error))
})
