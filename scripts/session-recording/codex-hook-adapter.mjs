#!/usr/bin/env node

import { execFileSync } from 'node:child_process'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { buildCodexManifestFileName, resolveManifestWriteTarget, writeCodexSessionManifest } from './write-codex-session-manifest.mjs'
import { scanObjectForSyntheticCanary, scanTextForSyntheticCanary } from './codex-metadata-scan.mjs'
import { verifyCodexPostRun } from './codex-postrun-verifier.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const repoRoot = resolve(__dirname, '..', '..')
// Issue #1408: single reuse surface for the publish-lane bounded policy. The
// safety decision itself (expected/current/local/verified/declared head
// comparison, allowed_paths_gate_status, force/tag/all/delete/mirror deny)
// lives in scripts/agent-guards/git_mutation_command_policy.py and MUST NOT be
// re-implemented here — see In Scope in Issue #1408.
const gitMutationPolicyScript = resolve(repoRoot, 'scripts', 'agent-guards', 'git_mutation_command_policy.py')
const defaultProducerScript = resolve(repoRoot, 'scripts', 'generate-session-manifest.mjs')
const configuredProducerScript = process.env.CODEX_SESSION_RECORDING_PRODUCER
  ? resolve(process.env.CODEX_SESSION_RECORDING_PRODUCER)
  : defaultProducerScript
const producerScript = configuredProducerScript.startsWith(repoRoot)
  ? configuredProducerScript
  : defaultProducerScript

// Issue #1428: shell command structure analyzer (SHELL_COMMAND_ANALYSIS_V1).
// Invoked out-of-process via execFile-style argv (no shell interpolation,
// no shell: true) with the command passed as JSON over stdin (data channel,
// not argv/shell-string), per In Scope 4 (analyzer invocation safety).
//
// CODEX_SHELL_COMMAND_ANALYZER (test-only override, PR #1441 High 3): lets
// the test suite substitute a fake analyzer script to exercise the strict
// SHELL_COMMAND_ANALYSIS_V1 response validation below (e.g. a regression
// where the analyzer emits a malformed command fact) without needing to
// break the real bounded-grammar analyzer. Same repoRoot-confinement
// pattern as CODEX_SESSION_RECORDING_PRODUCER — an override outside the
// repo is ignored and the production default is used instead.
const defaultShellCommandAnalyzerScript = resolve(repoRoot, 'scripts', 'agent-guards', 'shell_command_analysis.py')
const configuredShellCommandAnalyzerScript = process.env.CODEX_SHELL_COMMAND_ANALYZER
  ? resolve(process.env.CODEX_SHELL_COMMAND_ANALYZER)
  : defaultShellCommandAnalyzerScript
const shellCommandAnalyzerScript = configuredShellCommandAnalyzerScript.startsWith(repoRoot)
  ? configuredShellCommandAnalyzerScript
  : defaultShellCommandAnalyzerScript
const SHELL_COMMAND_ANALYSIS_SCHEMA = 'SHELL_COMMAND_ANALYSIS_V1'
const SHELL_ANALYZER_TIMEOUT_MS = 5000
const SHELL_ANALYZER_MAX_BUFFER = 1024 * 1024

const PUSH_COMMAND_KINDS = new Set(['git_push', 'rtk_git_push'])

// PR #1441 High 3: full SHELL_COMMAND_ANALYSIS_V1 command-fact validation —
// every key/type/enum/source-span shape the analyzer contract promises,
// mirrored from scripts/agent-guards/shell_command_analysis.py.
const COMMAND_FACT_KEYS = new Set([
  'command_kind',
  'executable_literalness',
  'subcommand_literalness',
  'remote_class',
  'refspec_class',
  'dangerous_flags',
  'execution_context',
  'source_span',
])
const VALID_COMMAND_KINDS = new Set(['git_push', 'rtk_git_push'])
const VALID_LITERALNESS = new Set(['literal', 'dynamic'])
const VALID_REMOTE_CLASSES = new Set(['absent', 'origin', 'other_literal', 'dynamic'])
const VALID_REFSPEC_CLASSES = new Set(['absent', 'head_to_literal_branch', 'other_literal', 'dynamic'])
const VALID_DANGEROUS_FLAGS = new Set(['force', 'tags', 'all', 'mirror', 'delete'])
const VALID_EXECUTION_CONTEXTS = new Set([
  'top_level',
  'list',
  'pipeline',
  'command_substitution',
  'process_substitution',
  'execution_carrier',
])
const VALID_ANALYZER_REASON_CODES = new Set([
  'parsed',
  'malformed_shell',
  'unsupported_construct',
  'dynamic_command_word',
  'analysis_timeout',
])

function isValidSourceSpan(span) {
  if (!span || typeof span !== 'object' || Array.isArray(span)) return false
  if (Object.keys(span).length !== 2) return false
  if (!Number.isInteger(span.start) || !Number.isInteger(span.end)) return false
  if (span.start < 0 || span.end < span.start) return false
  return true
}

function isValidCommandFact(fact) {
  if (!fact || typeof fact !== 'object' || Array.isArray(fact)) return false
  const keys = Object.keys(fact)
  if (keys.length !== COMMAND_FACT_KEYS.size) return false
  for (const key of keys) {
    if (!COMMAND_FACT_KEYS.has(key)) return false
  }
  if (typeof fact.command_kind !== 'string' || !VALID_COMMAND_KINDS.has(fact.command_kind)) return false
  if (typeof fact.executable_literalness !== 'string' || !VALID_LITERALNESS.has(fact.executable_literalness)) return false
  if (typeof fact.subcommand_literalness !== 'string' || !VALID_LITERALNESS.has(fact.subcommand_literalness)) return false
  if (typeof fact.remote_class !== 'string' || !VALID_REMOTE_CLASSES.has(fact.remote_class)) return false
  if (typeof fact.refspec_class !== 'string' || !VALID_REFSPEC_CLASSES.has(fact.refspec_class)) return false
  if (!Array.isArray(fact.dangerous_flags)) return false
  for (const flag of fact.dangerous_flags) {
    if (typeof flag !== 'string' || !VALID_DANGEROUS_FLAGS.has(flag)) return false
  }
  if (typeof fact.execution_context !== 'string' || !VALID_EXECUTION_CONTEXTS.has(fact.execution_context)) return false
  if (!isValidSourceSpan(fact.source_span)) return false
  return true
}

/**
 * PR #1441 High 3: validate the FULL SHELL_COMMAND_ANALYSIS_V1 shape,
 * including every command fact's keys/types/enums/source-span — not just
 * the top-level object/schema-name/commands-is-array/status-enum checks
 * this previously performed. A regression such as
 * `{"commands": [{"command_kind": 123}]}` must be normalized to
 * indeterminate/analysis_process_failed even when the top-level `status`
 * field says `ok` — never treated as allow.
 */
function isStructurallyValidAnalysis(parsed) {
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return false
  if (parsed.schema !== SHELL_COMMAND_ANALYSIS_SCHEMA) return false
  if (!Array.isArray(parsed.commands)) return false
  if (parsed.status !== 'ok' && parsed.status !== 'indeterminate') return false
  if (typeof parsed.reason_code !== 'string' || !VALID_ANALYZER_REASON_CODES.has(parsed.reason_code)) return false
  for (const fact of parsed.commands) {
    if (!isValidCommandFact(fact)) return false
  }
  return true
}

/**
 * Map an analyzer-internal indeterminate reason_code to the machine-readable
 * command_kind surfaced in the (still generic) remote_write_requires_approval
 * deny reason, per Issue #1428 In Scope 8. External consumers that only know
 * the pre-existing `remote_write_requires_approval` / `git_push` vocabulary
 * keep working (reason_code stays remote_write_requires_approval); the
 * command_kind field carries the finer-grained indeterminate classification.
 */
function mapIndeterminateReasonToCommandKind(analyzerReasonCode) {
  switch (analyzerReasonCode) {
    case 'dynamic_command_word':
      return 'dynamic_command_word'
    case 'unsupported_construct':
      return 'unsupported_execution_carrier'
    case 'analysis_timeout':
      return 'shell_command_analysis_timeout'
    case 'malformed_shell':
      return 'shell_command_parse_indeterminate'
    default:
      return 'shell_command_analysis_failed'
  }
}

/**
 * Run the bounded shell-command structure analyzer as a subprocess and
 * return its parsed SHELL_COMMAND_ANALYSIS_V1 result. Any failure mode
 * (non-zero exit, timeout, malformed JSON, schema mismatch, missing/invalid
 * commands array) is normalized to an indeterminate result with a
 * shell_command_analysis_failed-family command_kind — never fail-open
 * (Issue #1428 In Scope 1 / 2 / 8).
 */
function runShellCommandAnalyzer(command) {
  let stdout
  try {
    stdout = execFileSync('python3', [shellCommandAnalyzerScript], {
      input: JSON.stringify({ command }),
      encoding: 'utf8',
      timeout: SHELL_ANALYZER_TIMEOUT_MS,
      maxBuffer: SHELL_ANALYZER_MAX_BUFFER,
      stdio: ['pipe', 'pipe', 'pipe'],
    })
  } catch (err) {
    const timedOut = err && (err.signal === 'SIGTERM' || err.killed === true)
    return {
      status: 'indeterminate',
      commands: [],
      reason_code: timedOut ? 'analysis_timeout' : 'analysis_process_failed',
    }
  }

  let parsed
  try {
    parsed = JSON.parse(stdout)
  } catch {
    return { status: 'indeterminate', commands: [], reason_code: 'analysis_process_failed' }
  }

  // PR #1441 High 3: full schema validation — an `ok` status with a
  // malformed/incomplete command fact must still fail closed, never allow.
  if (!isStructurallyValidAnalysis(parsed)) {
    return { status: 'indeterminate', commands: [], reason_code: 'analysis_process_failed' }
  }
  return parsed
}

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

/** Issue #1408: cwd used for the publish-lane bounded policy classification.
 *  Falls back to the hook process's own cwd (which Codex sets to the tool
 *  invocation's working directory) when the payload does not carry one. */
function getCwd(payload) {
  return String(payload?.cwd ?? process.cwd())
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

/**
 * AC4 / Issue #1428: remote write commands - git push / rtk git push.
 *
 * Classification is delegated to the SHELL_COMMAND_ANALYSIS_V1 command
 * structure analyzer (scripts/agent-guards/shell_command_analysis.py)
 * instead of a substring/regex match against the raw command string
 * (Issue #1428 AC7). Non-executable occurrences of "git push" (quoted
 * argument data, search keywords, heredoc data with a quoted delimiter,
 * etc.) are never classified as remote write. Commands that would actually
 * execute `git push` / `git -C <path> push` / `rtk git push` — including
 * via pipeline, list, command substitution, or a known execution carrier
 * (bash -c / sh -c / eval / exec / env prefix / command wrapper) — are
 * still classified as remote write (AC3-AC6). Anything the analyzer cannot
 * statically resolve (dynamic command word, unsupported execution carrier,
 * parse failure, analyzer process failure) is fail-closed to remote write
 * as well (AC8) — never fail-open.
 */
function classifyRemoteWrite(command) {
  const analysis = runShellCommandAnalyzer(command)

  if (analysis.status === 'indeterminate') {
    return {
      reason_code: 'remote_write_requires_approval',
      command_kind: mapIndeterminateReasonToCommandKind(analysis.reason_code),
    }
  }

  const pushCommand = analysis.commands.find((entry) => PUSH_COMMAND_KINDS.has(entry?.command_kind))
  if (pushCommand) {
    return { reason_code: 'remote_write_requires_approval', command_kind: pushCommand.command_kind }
  }
  return null
}

/** Issue #1408 AC1/AC3/AC4: does `command` look like an `rtk git push` invocation
 *  (as opposed to a raw `git push` / `git -C <dir> push` / wrapper-bypass variant)?
 *  This is a narrow shape check only — it decides whether the publish-lane bounded
 *  policy CLI should be consulted at all; the policy itself owns every safety
 *  decision (force/tag/all/delete/mirror deny, head mismatch, allowed_paths gate). */
function looksLikeRtkGitPush(command) {
  return /^\s*rtk\s+git\s+push\b/.test(command)
}

/** Issue #1408 AC1/AC3: delegate the publish-lane safety decision for an
 *  `rtk git push origin HEAD:refs/heads/<active-branch>` command to the shared
 *  bounded policy in scripts/agent-guards/git_mutation_command_policy.py
 *  (single source of truth — see Issue #1402 / #1408 In Scope). Returns the
 *  parsed policy JSON, or null when the policy CLI is unavailable or the
 *  command does not match a recognized `rtk git` shape (fail-closed: callers
 *  keep the generic remote_write_requires_approval deny in that case). */
function classifyRtkGitPushPublishLane(command, cwd) {
  try {
    const stdout = execFileSync('python3', [
      gitMutationPolicyScript,
      '--command', command,
      '--cwd', cwd,
      '--boundary-layer', 'codex_hook_adapter_pretooluse',
    ], {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe'],
      timeout: 10_000,
    })
    const parsed = JSON.parse(stdout)
    if (parsed && typeof parsed === 'object' && parsed.status && parsed.status !== 'no_match') {
      return parsed
    }
    return null
  } catch {
    return null
  }
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
 *   'malformed_payload'             — Bash tool_input.command missing/non-string
 *   'forbidden_path'                — forbidden path access
 *   'secret_boundary_violation'     — secret-revealing command
 *   'remote_write_requires_approval'— remote write (git push etc.)
 */
function evaluateGuard(payload, eventName) {
  // Kill-switch flags apply regardless of tool/event shape (Stop /
  // SubagentStop payloads carry these too, with no tool_input at all).
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

  // PR #1441 Blocker 5: command-based classification (forbidden_path /
  // secret_boundary / remote_write) is ONLY evaluated inside the fixed
  // analyzer boundary: event ∈ {PreToolUse, PermissionRequest} AND
  // tool_name === 'Bash' AND typeof tool_input.command === 'string'. A
  // non-Bash tool call (or a non-PreToolUse/PermissionRequest event) never
  // reaches command-text classification at all — there is no `description`
  // fallback.
  if (eventName !== 'PreToolUse' && eventName !== 'PermissionRequest') {
    return null
  }
  if (payload?.tool_name !== 'Bash') {
    return null
  }
  if (typeof payload?.tool_input?.command !== 'string') {
    return {
      action: 'deny',
      reason_code: 'malformed_payload',
      command_kind: 'malformed_payload',
      message: `${eventName}: malformed_payload blocked (Bash tool_input.command missing or not a string)`,
    }
  }

  const rawCommand = payload.tool_input.command

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
    // Issue #1408 AC1/AC3/AC4: `rtk git push origin HEAD:refs/heads/<active-branch>`
    // with validated publish lane evidence bypasses the generic deny below. The
    // bounded decision is delegated to git_mutation_command_policy.py (single
    // source of truth) rather than re-implemented here. Raw `git push`,
    // `git -C <dir> push`, and wrapper-bypass variants never reach this branch
    // (looksLikeRtkGitPush returns false) and keep the existing deny (AC2).
    if (looksLikeRtkGitPush(command)) {
      const publishLane = classifyRtkGitPushPublishLane(command, getCwd(payload))
      if (publishLane && publishLane.status === 'allow') {
        // AC1: validated publish lane evidence — no denial, command passes through.
        return null
      }
      // Issue #1449 (PR #1479 OWNER review, P1 Blocker 1): the
      // initial_branch_create lane NEVER returns `allow` — the real
      // probe -> push -> readback transaction already ran synchronously
      // inside `classify_rtk_git_mutation` (via
      // `execute_initial_branch_create_transaction`), so the raw shell
      // command that triggered this hook is always denied afterward (it
      // would otherwise attempt a redundant/racy second push against the
      // same empty-expect lease). `reason_code` distinguishes an actual
      // completed-and-verified publish from a genuine safety stop.
      if (publishLane && publishLane.command_class === 'rtk_git_initial_branch_create') {
        const preview = redactCommandPreview(rawCommand)
        const remoteStateDetail = publishLane.remote_state_detail ?? {}
        return {
          action: 'deny',
          reason_code: 'initial_branch_create_transaction_result',
          command_kind: publishLane.command_class,
          message: `${eventName}: initial_branch_create_transaction_result `
            + `[transaction_status=${publishLane.reason_code}] `
            + `[remote_state=${remoteStateDetail.kind ?? publishLane.remote_state}] `
            + `[remote_oid=${remoteStateDetail.oid ?? 'null'}] `
            + `[error_category=${remoteStateDetail.error_category ?? 'null'}] `
            + `[local_head=${publishLane.local_head}] `
            + `blocked_command_preview="${preview}" `
            + '(the trusted transaction already executed the probe/push/readback — '
            + 'do not retry the raw command; inspect transaction_status above)',
        }
      }
      if (publishLane && publishLane.status === 'deny') {
        // AC3: PUBLISH_SAFETY_STOP_REPORT_V1-shaped reason — boundary_layer /
        // reason_code / head comparison values / required decisions.
        const preview = redactCommandPreview(rawCommand)
        const requiredDecisions = Array.isArray(publishLane.required_decisions)
          ? publishLane.required_decisions.join('; ')
          : ''
        return {
          action: 'deny',
          reason_code: 'publish_lane_safety_stop',
          command_kind: publishLane.command_class ?? 'rtk_git_push',
          message: `${eventName}: publish_lane_safety_stop `
            + `[boundary_layer=${publishLane.boundary_layer}] `
            + `[reason_code=${publishLane.reason_code}] `
            + `[expected_remote_head=${publishLane.expected_remote_head}] `
            + `[current_remote_head=${publishLane.current_remote_head}] `
            + `[local_head=${publishLane.local_head}] `
            + `[verified_head=${publishLane.verified_head}] `
            + `[declared_publish_head=${publishLane.declared_publish_head}] `
            + `[allowed_paths_gate_status=${publishLane.allowed_paths_gate_status}] `
            + `[required_decisions=${requiredDecisions}] `
            + `blocked_command_preview="${preview}"`,
        }
      }
      // publishLane === null: policy CLI unavailable, or a recognized-but-not-yet-
      // classified edge case. Fail-closed to the generic deny below (never allow
      // on ambiguous classification).
    }
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

  // Issue #1420 fix_delta AC10 / Issue #1546: resolve the real write target
  // BEFORE producing the manifest so evidence_ref reflects the actual write
  // location instead of a fixed string that can diverge from it. Issue
  // #1546: the production default (no CODEX_HOOK_MANIFEST_ROOT override) is
  // now the canonical external per-user state root -- never the repository
  // tree. A CODEX_HOOK_MANIFEST_ROOT override (test isolation only) is
  // still honored, and is validated fail-before-mutation by
  // resolveCodexSessionManifestRoot (via resolveManifestWriteTarget).
  const manifestWriteResult = resolveManifestWriteTarget({
    repoRoot,
    eventName,
    fileName,
  })
  const evidenceSourceRef = manifestWriteResult.relativePath
  const manifest = produceManifest(eventName, payload, evidenceSourceRef)
  const stdoutFindings = scanTextForSyntheticCanary(JSON.stringify(manifest))
  const stderrFindings = []

  if (metadataFindings.length > 0 || stdoutFindings.length > 0 || stderrFindings.length > 0) {
    throw new Error(`${eventName}: synthetic canary leaked into public surface`)
  }

  writeCodexSessionManifest({
    manifest,
    repoRoot,
    eventName,
    fileName,
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
