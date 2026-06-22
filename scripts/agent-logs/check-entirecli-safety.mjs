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

import { checkEntireCLISafety, redactFingerprint } from './lib/entirecli-safety.mjs'
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
 * Check if entire binary is available.
 * @param {string} cwd
 * @returns {boolean}
 */
function detectEntireBinary(cwd) {
  const result = runCommand('entire', ['version'], cwd)
  return result !== null
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
 * Get flat git config as key→value map.
 * @param {string} cwd
 * @returns {{ config: Record<string, string>, errors: string[] }}
 */
function loadGitConfig(cwd) {
  const output = runCommand('git', ['config', '--list'], cwd)
  if (output === null) {
    return { config: {}, errors: ['git config --list failed'] }
  }
  const config = /** @type {Record<string, string>} */ ({})
  const errors = /** @type {string[]} */ ([])
  for (const line of output.split('\n')) {
    const eqIndex = line.indexOf('=')
    if (eqIndex < 0) {
      if (line.trim()) errors.push(`malformed config line: ${line.length} chars`)
      continue
    }
    const key = line.slice(0, eqIndex).trim().toLowerCase()
    const value = line.slice(eqIndex + 1)
    config[key] = value
  }
  return { config, errors }
}

/**
 * Try to parse a JSON file, returning empty object on failure.
 * @param {string} filePath
 * @returns {Record<string, unknown>}
 */
function tryParseJsonFile(filePath) {
  try {
    const raw = readFileSync(filePath, 'utf-8')
    return JSON.parse(raw)
  } catch {
    return {}
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
 * Determine checkpoint remote visibility (heuristic).
 * Returns 'local_only' if remote URL appears to be a local path.
 * Without live GitHub API access, returns 'unknown' for GitHub remotes.
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
  // Without live GitHub API access, we cannot determine visibility
  return 'unknown'
}

async function main() {
  const options = parseArgs(process.argv.slice(2), OPTION_SPEC)
  const repoRoot = resolve(options.repoRoot)

  // Collect all input
  const entireBinaryPresent = detectEntireBinary(repoRoot)
  const entireDirPresent = existsSync(resolve(repoRoot, '.entire'))
  const entireHooksPresent = existsSync(resolve(repoRoot, '.entire', 'hooks'))
  const localRefs = listLocalRefs(repoRoot)
  const remoteBranches = listRemoteBranches(repoRoot)
  const checkpointTrailerPresent = detectCheckpointTrailers(repoRoot)
  const tokenEnvPresent = typeof process.env.ENTIRE_CHECKPOINT_TOKEN === 'string'

  const baseSettings = tryParseJsonFile(resolve(repoRoot, '.entire', 'settings.json'))
  const localSettings = tryParseJsonFile(resolve(repoRoot, '.entire', 'settings.local.json'))

  const { config: gitConfig, errors: gitConfigParseErrors } = loadGitConfig(repoRoot)

  // Determine checkpoint_remote from settings (merged)
  const mergedSettings = { ...baseSettings, ...localSettings }
  const checkpointRemoteRaw = typeof mergedSettings.checkpoint_remote === 'string'
    ? mergedSettings.checkpoint_remote
    : null

  // Get checkpoint remote URL from git config (do NOT emit raw URL)
  const checkpointRemoteUrl = checkpointRemoteRaw
    ? (gitConfig[`remote.${checkpointRemoteRaw}.url`] ?? null)
    : null

  const checkpointRemoteVisibility = detectCheckpointRemoteVisibility(checkpointRemoteUrl)

  // Build redacted diagnostics only — no raw values
  const diagnosticStrings = [
    checkpointRemoteRaw ? `checkpoint_remote: ${redactFingerprint(checkpointRemoteRaw)}` : '',
    checkpointRemoteUrl ? `remote_url_fingerprint: ${redactFingerprint(checkpointRemoteUrl)}` : '',
    ...gitConfigParseErrors,
  ].filter(Boolean)

  const result = checkEntireCLISafety({
    entireBinaryPresent,
    entireDirPresent,
    entireHooksPresent,
    localRefs,
    checkpointTrailerPresent,
    tokenEnvPresent,
    baseSettings,
    localSettings,
    checkpointRemote: checkpointRemoteRaw,
    checkpointRemoteVisibility,
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
