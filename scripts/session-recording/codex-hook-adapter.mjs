#!/usr/bin/env node

import { execFileSync } from 'node:child_process'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { buildCodexManifestFileName, writeCodexSessionManifest } from './write-codex-session-manifest.mjs'
import { scanObjectForSyntheticCanary, scanTextForSyntheticCanary } from './codex-metadata-scan.mjs'
import { verifyCodexPostRun } from './codex-postrun-verifier.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const repoRoot = resolve(__dirname, '..', '..')
const producerScript = process.env.CODEX_SESSION_RECORDING_PRODUCER
  ? resolve(process.env.CODEX_SESSION_RECORDING_PRODUCER)
  : resolve(repoRoot, 'scripts', 'generate-session-manifest.mjs')

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

function matchesForbiddenPath(command) {
  return /(^|\s)(assets\/|LICENSES\/|\.env(\.|$))/.test(command)
}

function evaluateGuard(payload, eventName) {
  const command = getCommand(payload)

  if (payload?.public_checkpoint_enabled === true) {
    return `${eventName}: public checkpoint is forbidden`
  }
  if (payload?.unknown_visibility_mapping === true) {
    return `${eventName}: unknown visibility mapping must fail closed`
  }
  if (payload?.secrets_mode && payload.secrets_mode !== 'none') {
    return `${eventName}: secrets_mode must remain none`
  }
  if (payload?.forbidden_path_touched === true || matchesForbiddenPath(command)) {
    return `${eventName}: forbidden path access blocked`
  }
  if (/\bgit push\b|\bgh secret\b|\bprintenv\b|\benv\b/.test(command)) {
    return `${eventName}: destructive or secret-revealing command blocked`
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

  const written = writeCodexSessionManifest({
    manifest,
    repoRoot,
    eventName,
    fileName,
  })

  const verification = verifyCodexPostRun(payload)
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
  } catch (error) {
    if (event === 'PreToolUse') {
      emitJson(denyPreToolUse(`Malformed ${event} payload blocked by hook.`))
      return
    }
    if (event === 'PermissionRequest') {
      emitJson(denyPermissionRequest(`Malformed ${event} payload blocked by hook.`))
      return
    }
    emitJson(stopEventOutput(`Malformed ${event} payload blocked by hook.`))
    return
  }

  const reason = evaluateGuard(payload, event)
  if (reason) {
    if (event === 'PreToolUse') {
      emitJson(denyPreToolUse(reason))
      return
    }
    if (event === 'PermissionRequest') {
      emitJson(denyPermissionRequest(reason))
      return
    }
    emitJson(stopEventOutput(reason))
    return
  }

  if (event === 'Stop' || event === 'SubagentStop') {
    emitJson(runManifestFlow(event, payload))
    return
  }

  if (event === 'PreToolUse' || event === 'PermissionRequest') {
    return
  }

  throw new Error(`Unsupported event: ${event}`)
}

main().catch((error) => {
  process.stderr.write(`${error.message}\n`)
  process.exit(1)
})
