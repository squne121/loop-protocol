#!/usr/bin/env node

import { execFileSync } from 'node:child_process'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { buildCodexManifestFileName, writeCodexSessionManifest } from './write-codex-session-manifest.mjs'
import { scanObjectForSyntheticCanary, scanTextForSyntheticCanary } from './codex-metadata-scan.mjs'
import { verifyCodexPostRun } from './codex-postrun-verifier.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const repoRoot = resolve(__dirname, '..', '..')
const defaultProducerScript = resolve(repoRoot, 'scripts', 'generate-session-manifest.mjs')
const configuredProducerScript = process.env.CODEX_SESSION_RECORDING_PRODUCER
  ? resolve(process.env.CODEX_SESSION_RECORDING_PRODUCER)
  : defaultProducerScript
const producerScript = configuredProducerScript.startsWith(repoRoot)
  ? configuredProducerScript
  : defaultProducerScript

function parseArgs(argv) {
  const args = { event: null }
  for (let index = 2; index < argv.length; index += 1) {
    const token = argv[index]
    if (token === '--event') {
      args.event = argv[index + 1]
      index += 1
      continue
    }
    throw new Error(`Unknown option: ${token}`)
  }
  if (!args.event) {
    throw new Error('--event is required')
  }
  return args
}

function emitJson(payload) {
  process.stdout.write(JSON.stringify(payload))
}

function stopEventOutput(reason) {
  return {
    continue: false,
    stopReason: reason,
  }
}

function denyPreToolUse(reason) {
  return {
    hookSpecificOutput: {
      hookEventName: 'PreToolUse',
      permissionDecision: 'deny',
      permissionDecisionReason: reason,
    },
  }
}

function denyPermissionRequest(reason) {
  return {
    hookSpecificOutput: {
      hookEventName: 'PermissionRequest',
      decision: {
        behavior: 'deny',
        message: reason,
      },
    },
  }
}

async function readJsonFromStdin() {
  let text = ''
  process.stdin.setEncoding('utf8')
  for await (const chunk of process.stdin) {
    text += chunk
  }
  if (!text.trim()) {
    throw new Error('empty stdin')
  }
  return JSON.parse(text)
}

function getCommand(payload) {
  return String(payload?.tool_input?.command ?? payload?.tool_input?.description ?? '')
}

function sanitizeStopReason(eventName) {
  return `${eventName}: Codex session recording failed; see private local log.`
}

function matchesForbiddenPath(command) {
  return /(^|[^A-Za-z0-9_./-])((assets|LICENSES)\/|(?:[^\s'"]*\/)?\.env(?:\.[^\s'"]+)*)/.test(command)
}

// ---------------------------------------------------------------------------
// Command classification helpers (AC2-AC5, #783)
// Each classifier returns a structured denial descriptor or null.
// Priority: forbidden_path > secret_boundary > remote_write > read_only pass-through
// ---------------------------------------------------------------------------

/** AC3: secret-revealing commands - gh secrets / printenv / env dump */
function classifySecretBoundary(command) {
  // gh secret list/set/get or gh api .../secrets
  if (/\bgh\s+(?:secret\b|api\b[^\n]*\bsecrets\b)/.test(command)) {
    return {
      reason_code: 'secret_boundary_violation',
      command_kind: /\bgh\s+api\b/.test(command) ? 'gh_api_actions_secrets' : 'gh_secret',
    }
  }
  // printenv - dumps all env vars including secrets
  if (/\bprintenv\b/.test(command)) {
    return { reason_code: 'secret_boundary_violation', command_kind: 'printenv' }
  }
  // bare env invocation as a standalone dump command (not env FOO=bar cmd prefix)
  if (/(?:^|[;&|]\s*|\n\s*)env\s*$/.test(command)) {
    return { reason_code: 'secret_boundary_violation', command_kind: 'env_dump' }
  }
  // python -c with sensitive env variable access patterns
  if (/\bpython[23]?\s+-c\s+['"][^'"]*os\.environ/.test(command)) {
    return { reason_code: 'secret_boundary_violation', command_kind: 'python_os_environ' }
  }
  return null
}

/** AC4: remote write commands - git push variants */
function classifyRemoteWrite(command) {
  if (/\bgit(?:\s+-C\s+\S+)?\s+push\b/.test(command)) {
    return { reason_code: 'remote_write_requires_approval', command_kind: 'git_push' }
  }
  return null
}

/** AC2: read-only investigation commands that must NOT be blocked.
 *  This function is called only AFTER secret/remote classifiers have been
 *  applied to the stripped command (env prefix already removed by stripEnvPrefix).
 *  Returns true for commands that are safe read-only investigations. */
function isReadonlyInvestigation(_command) {
  // Placeholder — all env-prefix handling is now done via stripEnvPrefix.
  // This function remains for future read-only pass-through extensions.
  return false
}

/** Strip `env VAR=val ...` prefix from a command string.
 *  Returns { stripped: string, isEnvDump: boolean }.
 *
 *  Cases:
 *    "env"                           → isEnvDump: true
 *    "env -0"                        → isEnvDump: true
 *    "env --ignore-environment"      → isEnvDump: true
 *    "env VAR=val cmd arg"           → stripped: "cmd arg", isEnvDump: false
 *    anything else starting with env → stripped: original, isEnvDump: false
 */
function stripEnvPrefix(command) {
  const trimmed = command.trim()
  // bare "env" with no args (possibly trailing whitespace) → env dump
  if (/^env\s*$/.test(trimmed)) {
    return { stripped: trimmed, isEnvDump: true }
  }
  // "env" followed by a flag that means dump all vars
  if (/^env\s+(-0|--ignore-environment|-i)(\s|$)/.test(trimmed)) {
    return { stripped: trimmed, isEnvDump: true }
  }
  // "env VAR=val ... cmd ..." — strip all leading VAR=val tokens
  const envPrefixMatch = /^env\s+((?:[A-Za-z_][A-Za-z0-9_]*=[^\s]*\s+)+)(.+)$/.exec(trimmed)
  if (envPrefixMatch) {
    return { stripped: envPrefixMatch[2], isEnvDump: false }
  }
  // "env" followed by something that is not VAR=val assignments → treat as env dump
  if (/^env\s+/.test(trimmed)) {
    return { stripped: trimmed, isEnvDump: true }
  }
  return { stripped: trimmed, isEnvDump: false }
}

/** Produce a redacted preview of a command for deny reason strings (AC7). */
function redactCommandPreview(command) {
  // Redact secret-like tokens before truncation
  let redacted = command
    .replace(/\bsk-[A-Za-z0-9_-]{10,}/g, 'sk-[REDACTED]')
    .replace(/\bghp_[A-Za-z0-9_]{10,}/g, 'ghp_[REDACTED]')
    .replace(/\bgithub_pat_[A-Za-z0-9_]{10,}/g, 'github_pat_[REDACTED]')
    .replace(/Authorization:\s*Bearer\s+[^\s"']+/gi, 'Authorization: Bearer [REDACTED]')
    .replace(/\b[A-Z_]*(TOKEN|SECRET|PASSWORD|KEY)=[^\s"']+/g, (match, keyword) => `${keyword}=[REDACTED]`)
    .replace(/--token\s+[^\s"']+/g, '--token [REDACTED]')
    .replace(/-H\s+"Authorization:[^"]+/g, '-H "Authorization: [REDACTED]')
    .replace(/-H\s+'Authorization:[^']+/g, "-H 'Authorization: [REDACTED]")

  const truncated = redacted.length > 80
    ? `${redacted.slice(0, 80).replace(/\s+/g, ' ')}...`
    : redacted.replace(/\s+/g, ' ')
  return truncated
}

/**
 * evaluateGuard returns a structured object or null.
 * Returns:
 *   null                                        — no decision (allow pass-through)
 *   { action: 'deny', reason_code, command_kind, message } — deny decision
 *   { action: 'no_decision', reason_code, command_kind, message } — explicit no_decision
 *
 * reason_code values:
 *   'public_checkpoint'             — public_checkpoint_enabled flag
 *   'unknown_visibility_mapping'    — unknown_visibility_mapping flag
 *   'secrets_mode'                  — secrets_mode != 'none'
 *   'forbidden_path'                — forbidden path access
 *   'secret_boundary_violation'     — secret-revealing command
 *   'remote_write_requires_approval'— remote write (git push etc.)
 */
function evaluateGuard(payload, eventName) {
  const rawCommand = getCommand(payload)

  if (payload?.public_checkpoint_enabled === true) {
    return {
      action: 'deny',
      reason_code: 'public_checkpoint',
      command_kind: 'public_checkpoint',
      message: `${eventName}: public checkpoint is forbidden`,
    }
  }
  if (payload?.unknown_visibility_mapping === true) {
    return {
      action: 'deny',
      reason_code: 'unknown_visibility_mapping',
      command_kind: 'unknown_visibility_mapping',
      message: `${eventName}: unknown visibility mapping must fail closed`,
    }
  }
  if (payload?.secrets_mode && payload.secrets_mode !== 'none') {
    return {
      action: 'deny',
      reason_code: 'secrets_mode',
      command_kind: 'secrets_mode',
      message: `${eventName}: secrets_mode must remain none`,
    }
  }
  if (payload?.forbidden_path_touched === true || matchesForbiddenPath(rawCommand)) {
    return {
      action: 'deny',
      reason_code: 'forbidden_path',
      command_kind: 'forbidden_path',
      message: `${eventName}: forbidden path access blocked`,
    }
  }

  // Normalize env VAR=val prefix before classification.
  // env dump variants (bare "env", "env -0", etc.) are denied immediately.
  const { stripped: command, isEnvDump } = stripEnvPrefix(rawCommand)
  if (isEnvDump) {
    const preview = redactCommandPreview(rawCommand)
    return {
      action: 'deny',
      reason_code: 'secret_boundary_violation',
      command_kind: 'env_dump',
      message: `${eventName}: secret_boundary_violation [command_kind=env_dump] blocked_command_preview="${preview}"`,
    }
  }

  // AC3: secret boundary - highest priority deny among command classifiers
  const secretDenial = classifySecretBoundary(command)
  if (secretDenial) {
    const preview = redactCommandPreview(rawCommand)
    return {
      action: 'deny',
      reason_code: 'secret_boundary_violation',
      command_kind: secretDenial.command_kind,
      message: `${eventName}: secret_boundary_violation [command_kind=${secretDenial.command_kind}] blocked_command_preview="${preview}"`,
    }
  }

  // AC4: remote write - no_decision on PermissionRequest, deny on PreToolUse
  const remoteWriteDenial = classifyRemoteWrite(command)
  if (remoteWriteDenial) {
    const preview = redactCommandPreview(rawCommand)
    return {
      action: 'no_decision',
      reason_code: 'remote_write_requires_approval',
      command_kind: remoteWriteDenial.command_kind,
      message: `${eventName}: remote_write_requires_approval [command_kind=${remoteWriteDenial.command_kind}] blocked_command_preview="${preview}"`,
    }
  }

  // AC2: read-only investigation passes through (no deny)
  if (isReadonlyInvestigation(command)) {
    return null
  }

  return null
}

function buildProducerArgs(eventName, evidenceSourceRef) {
  return [
    producerScript,
    '--repository', 'squne121/loop-protocol',
    '--phase-main-loop', 'impl',
    '--phase-ledger-phase', 'post_commit_verification',
    '--phase-instance-id', 'issue-768:impl:001',
    '--actor-type', 'ai_agent',
    '--actor-name', `codex-${eventName.toLowerCase()}-hook`,
    '--evidence-source-kind', 'artifact',
    '--evidence-source-ref', evidenceSourceRef,
    '--evidence-visibility', 'private_artifact',
    '--format', 'json',
    '--validate',
  ]
}

function produceManifest(eventName, payload, evidenceSourceRef) {
  const stdout = execFileSync(process.execPath, buildProducerArgs(eventName, evidenceSourceRef), {
    cwd: repoRoot,
    encoding: 'utf8',
    stdio: ['pipe', 'pipe', 'pipe'],
  })
  const manifest = JSON.parse(stdout)
  manifest.secret_policy.runtime_boundary = {
    attested: true,
    evidence_ref: evidenceSourceRef,
  }
  manifest.token_usage = {
    availability: 'unavailable',
    source: 'none',
    prompt: null,
    completion: null,
    total: null,
  }
  return manifest
}

function runManifestFlow(eventName, payload) {
  const metadataFindings = scanObjectForSyntheticCanary(payload)
  const fileName = buildCodexManifestFileName()
  const evidenceSourceRef = `tmp/session-manifests/codex/${eventName.toLowerCase()}/${fileName}`
  const manifest = produceManifest(eventName, payload, evidenceSourceRef)
  const stdoutFindings = scanTextForSyntheticCanary(JSON.stringify(manifest))
  const stderrFindings = []

  if (metadataFindings.length > 0 || stdoutFindings.length > 0 || stderrFindings.length > 0) {
    throw new Error(`${eventName}: synthetic canary leaked into public surface`)
  }

  // AC2: when CODEX_HOOK_MANIFEST_ROOT is set, honor it as the manifest write-target
  // override (used by the test suite to isolate per-test manifest directories under
  // pytest-xdist parallel execution). Unset/empty falls back to the production default.
  const manifestRootOverride = process.env.CODEX_HOOK_MANIFEST_ROOT || undefined

  writeCodexSessionManifest({
    manifest,
    repoRoot,
    eventName,
    fileName,
    manifestRoot: manifestRootOverride,
  })

  const verification = verifyCodexPostRun(payload, { repoRoot })
  if (!verification.ok) {
    return stopEventOutput(`${eventName}: ${verification.failures.join(', ')}`)
  }
  return { continue: true }
}

async function main() {
  const { event } = parseArgs(process.argv)
  let payload
  try {
    payload = await readJsonFromStdin()
  } catch {
    if (event === 'PreToolUse') {
      emitJson(denyPreToolUse(`Malformed ${event} payload blocked by hook.`))
      return
    }
    if (event === 'PermissionRequest') {
      emitJson(denyPermissionRequest(`Malformed ${event} payload blocked by hook.`))
      return
    }
    // Stop / SubagentStop: session recording failure → best-effort telemetry, continue:true
    // Do NOT emit stopEventOutput (continue:false) for recording failures (AC3)
    process.stderr.write(`[codex-hook-adapter] warn: malformed ${event} payload — session recording skipped (best-effort)\n`)
    emitJson({ continue: true })
    return
  }

  const guardResult = evaluateGuard(payload, event)
  if (guardResult !== null) {
    if (event === 'PreToolUse') {
      // PreToolUse: deny for all deny/no_decision guard results (remote_write blocks PreToolUse)
      emitJson(denyPreToolUse(guardResult.message))
      return
    }
    if (event === 'PermissionRequest') {
      // AC5 (#874): remote_write_requires_approval is no_decision on PermissionRequest
      // (PreToolUse side still denies; permission-request side defers to Codex runtime).
      // Other critical denials (secret_boundary_violation, forbidden_path, public_checkpoint,
      // secrets_mode) remain as deny on PermissionRequest.
      if (guardResult.reason_code === 'remote_write_requires_approval') {
        // no_decision: emit nothing (exit 0, no stdout JSON)
        return
      }
      emitJson(denyPermissionRequest(guardResult.message))
      return
    }
    emitJson(stopEventOutput(guardResult.message))
    return
  }

  if (event === 'Stop' || event === 'SubagentStop') {
    // AC3: session recording failure → best-effort telemetry, always continue:true
    let result
    try {
      result = runManifestFlow(event, payload)
    } catch (manifestErr) {
      process.stderr.write(`[codex-hook-adapter] warn: ${event} manifest flow failed (best-effort): ${String(manifestErr?.message ?? 'unknown')}\n`)
      result = { continue: true }
    }
    emitJson(result)
    return
  }

  if (event === 'PreToolUse' || event === 'PermissionRequest') {
    return
  }

  throw new Error(`Unsupported event: ${event}`)
}

main().catch((err) => {
  const eventIndex = process.argv.indexOf('--event')
  const eventName = eventIndex >= 0 ? process.argv[eventIndex + 1] : null
  if (eventName === 'Stop' || eventName === 'SubagentStop') {
    // AC3: session recording failure → best-effort telemetry, continue:true (never block session)
    process.stderr.write(`[codex-hook-adapter] warn: ${eventName} hook failed (best-effort, continuing): ${String(err?.message ?? 'unknown')}\n`)
    emitJson({ continue: true })
    process.exit(0)
  }
  if (eventName === 'PreToolUse') {
    emitJson(denyPreToolUse(`Malformed ${eventName} payload blocked by hook.`))
    process.exit(0)
  }
  if (eventName === 'PermissionRequest') {
    emitJson(denyPermissionRequest(`Malformed ${eventName} payload blocked by hook.`))
    process.exit(0)
  }
  process.stderr.write('Codex session recording hook failed.\n')
  process.exit(1)
})
