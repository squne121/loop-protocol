/**
 * runtime-lane-completion-hook-wiring.test.ts
 *
 * #2489 AC1 / AC2 / AC3 (#1939 AC4 相当):
 *
 * - AC1: runtime_lane の provenance は launcher-set 環境変数
 *   CLAUDE_GPT_CLAUDE_BIN の有無のみに由来し、actor.name や transcript から
 *   推測しないことを確認する。
 * - AC2 (P0-1 fix, PR #2496 OWNER review issuecomment-5546874328): Stop /
 *   StopFailure は turn-level event であり、session/run-level の
 *   completion_outcome / completion_source をこの hook 経路からは絶対に
 *   設定しない。代わりに turn-level evidence である hook_event.event_type
 *   （と StopFailure の場合の optional hook_event.error_type）のみを導出
 *   することを確認する。
 * - AC1/AC2: buildProducerArgs が --runtime-lane / --hook-event-type /
 *   --hook-error-type を条件付きで含み、--completion-outcome /
 *   --completion-source は一切含まないことを確認する。
 * - AC1/AC2/AC3: scripts/generate-session-manifest.mjs（producer 本体）が
 *   これらの CLI 引数をそのまま受け取り、schema validator を通ることを
 *   実プロセス起動で確認する（round-trip）。
 */

import { execFileSync } from 'child_process'
import { readFileSync } from 'fs'
import { resolve } from 'path'
import { describe, expect, it } from 'vitest'

import {
  buildProducerArgs,
  resolveHookErrorType,
  resolveHookEventType,
  resolveRuntimeLane,
} from '../../.claude/hooks/generate_session_manifest_from_hook.mjs'

const REPO_ROOT = resolve(__dirname, '..', '..')
const PRODUCER_SCRIPT = resolve(REPO_ROOT, 'scripts', 'generate-session-manifest.mjs')
const SCHEMA_PATH = resolve(REPO_ROOT, 'docs', 'schemas', 'agent-session-manifest.schema.json')

function runProducer(args: string[]) {
  try {
    const stdout = execFileSync(process.execPath, [PRODUCER_SCRIPT, ...args], {
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
    })
    return { exitCode: 0, stdout, stderr: '' }
  } catch (error) {
    const err = error as { status?: number; stdout?: string; stderr?: string }
    return { exitCode: err.status ?? 1, stdout: err.stdout || '', stderr: err.stderr || '' }
  }
}

const BASE_PRODUCER_ARGS = [
  '--repository', 'squne121/loop-protocol',
  '--issue', '2489',
  '--phase-main-loop', 'impl',
  '--phase-ledger-phase', 'post_commit_verification',
  '--phase-instance-id', 'issue-2489:impl:001',
  '--actor-type', 'ai_agent',
  '--actor-name', 'claude-code-hook',
  '--evidence-source-kind', 'artifact',
  '--evidence-source-ref', 'artifacts/session-manifest-runtime/manifests/example.json',
  '--evidence-visibility', 'private_artifact',
  '--format', 'json',
]

describe('resolveRuntimeLane (#2489 AC1) — CLAUDE_GPT_CLAUDE_BIN presence only, never actor.name/transcript', () => {
  it('GIVEN CLAUDE_GPT_CLAUDE_BIN is set WHEN resolved THEN it returns claude_gpt', () => {
    expect(resolveRuntimeLane({ CLAUDE_GPT_CLAUDE_BIN: '/usr/local/bin/claude-gpt' })).toBe('claude_gpt')
  })

  it('GIVEN CLAUDE_GPT_CLAUDE_BIN is absent WHEN resolved THEN it returns native_claude_code', () => {
    expect(resolveRuntimeLane({})).toBe('native_claude_code')
  })

  it('GIVEN CLAUDE_GPT_CLAUDE_BIN is an empty string WHEN resolved THEN it returns native_claude_code (not treated as present)', () => {
    expect(resolveRuntimeLane({ CLAUDE_GPT_CLAUDE_BIN: '   ' })).toBe('native_claude_code')
  })

  it('GIVEN a null/undefined env WHEN resolved THEN it fails closed to native_claude_code rather than throwing', () => {
    expect(resolveRuntimeLane(undefined as unknown as NodeJS.ProcessEnv)).toBe('native_claude_code')
  })
})

describe('resolveHookEventType (#2489 AC2 P0-1 fix) — turn-level hook_event.event_type only, never completion fields', () => {
  it('GIVEN Stop WHEN resolved THEN it returns Stop (recorded as hook_event.event_type only)', () => {
    expect(resolveHookEventType('Stop')).toBe('Stop')
  })

  it('GIVEN StopFailure WHEN resolved THEN it returns StopFailure (recorded as hook_event.event_type only)', () => {
    expect(resolveHookEventType('StopFailure')).toBe('StopFailure')
  })

  it('GIVEN an unrecognized hook event name WHEN resolved THEN it returns null (not recorded)', () => {
    expect(resolveHookEventType('SomeFutureEvent')).toBeNull()
  })
})

describe('resolveHookErrorType (#2489 AC2 P0-1 fix) — StopFailure-only optional turn-level error taxonomy', () => {
  it('GIVEN StopFailure with a recognized upstream error.type WHEN resolved THEN it returns that value', () => {
    expect(resolveHookErrorType('StopFailure', { error: { type: 'rate_limit' } })).toBe('rate_limit')
    expect(resolveHookErrorType('StopFailure', { error: { type: 'overloaded' } })).toBe('overloaded')
  })

  it('GIVEN StopFailure with a missing or unrecognized error type WHEN resolved THEN it normalizes to unknown (not omitted)', () => {
    expect(resolveHookErrorType('StopFailure', {})).toBe('unknown')
    expect(resolveHookErrorType('StopFailure', { error: { type: 'not_a_real_taxonomy_value' } })).toBe('unknown')
  })

  it('GIVEN a non-StopFailure event WHEN resolved THEN it returns null regardless of payload content', () => {
    expect(resolveHookErrorType('Stop', { error: { type: 'rate_limit' } })).toBeNull()
    expect(resolveHookErrorType('PostToolUse', {})).toBeNull()
  })
})

describe('buildProducerArgs (#2489 AC1/AC2) — conditional CLI arg wiring', () => {
  const baseParams = {
    producerScript: PRODUCER_SCRIPT,
    repository: 'squne121/loop-protocol',
    phaseInfo: { mainLoop: 'impl', ledgerPhase: 'post_commit_verification' },
    phaseInstanceId: 'issue-2489:impl:001',
    actorType: 'ai_agent',
    actorName: 'claude-code-hook',
    evidenceSourceKind: 'artifact',
    evidenceSourceRef: 'artifacts/session-manifest-runtime/manifests/example.json',
    evidenceVisibility: 'private_artifact',
    sessionId: null,
    resolvedIssueNumber: 2489,
  }

  it('GIVEN runtimeLane/hookEventType/hookErrorType provided WHEN built THEN all three CLI flags are included and completion-outcome/-source are never included', () => {
    const args = buildProducerArgs({
      ...baseParams,
      runtimeLane: 'claude_gpt',
      hookEventType: 'StopFailure',
      hookErrorType: 'rate_limit',
    })
    expect(args[args.indexOf('--runtime-lane') + 1]).toBe('claude_gpt')
    expect(args[args.indexOf('--hook-event-type') + 1]).toBe('StopFailure')
    expect(args[args.indexOf('--hook-error-type') + 1]).toBe('rate_limit')
    expect(args).not.toContain('--completion-outcome')
    expect(args).not.toContain('--completion-source')
  })

  it('GIVEN runtimeLane/hookEventType/hookErrorType are null (no defaults provided) WHEN built THEN none of the flags are included', () => {
    const args = buildProducerArgs({ ...baseParams })
    expect(args).not.toContain('--runtime-lane')
    expect(args).not.toContain('--hook-event-type')
    expect(args).not.toContain('--hook-error-type')
    expect(args).not.toContain('--completion-outcome')
    expect(args).not.toContain('--completion-source')
  })
})

describe('producer CLI round-trip (#2489 AC1/AC2/AC3) — schema validator accepts the new optional fields', () => {
  it('GIVEN --runtime-lane/--hook-event-type/--hook-error-type WHEN the producer runs THEN it emits a schema-valid manifest with hook_event set and no root completion fields', () => {
    const result = runProducer([
      ...BASE_PRODUCER_ARGS,
      '--runtime-lane', 'native_claude_code',
      '--hook-event-type', 'StopFailure',
      '--hook-error-type', 'overloaded',
      '--validate',
    ])
    expect(result.exitCode).toBe(0)
    const manifest = JSON.parse(result.stdout)
    expect(manifest.runtime_lane).toBe('native_claude_code')
    expect(manifest.hook_event).toEqual({ event_type: 'StopFailure', error_type: 'overloaded' })
    expect(manifest.completion_outcome).toBeUndefined()
    expect(manifest.completion_source).toBeUndefined()
  })

  it('GIVEN --completion-outcome/--completion-source (launcher/reconciliation caller, not the hook wrapper) WHEN the producer runs THEN both are accepted together (dependentRequired pair)', () => {
    const result = runProducer([
      ...BASE_PRODUCER_ARGS,
      '--completion-outcome', 'completed',
      '--completion-source', 'reconciliation',
      '--validate',
    ])
    expect(result.exitCode).toBe(0)
    const manifest = JSON.parse(result.stdout)
    expect(manifest.completion_outcome).toBe('completed')
    expect(manifest.completion_source).toBe('reconciliation')
  })

  it('GIVEN only --completion-outcome without --completion-source WHEN the producer runs THEN schema validation fails closed (dependentRequired pair violated)', () => {
    const result = runProducer([
      ...BASE_PRODUCER_ARGS,
      '--completion-outcome', 'completed',
      '--validate',
    ])
    expect(result.exitCode).toBe(1)
  })

  it('GIVEN --runtime-lane codex_cli WHEN the producer runs THEN it validates as a schema-level enum member (AC3: producer wiring itself stays out of scope)', () => {
    const result = runProducer([
      ...BASE_PRODUCER_ARGS,
      '--runtime-lane', 'codex_cli',
      '--validate',
    ])
    expect(result.exitCode).toBe(0)
    const manifest = JSON.parse(result.stdout)
    expect(manifest.runtime_lane).toBe('codex_cli')
  })

  it('GIVEN an invalid --runtime-lane value WHEN the producer runs THEN it fails closed (exit 1) before touching the schema validator', () => {
    const result = runProducer([
      ...BASE_PRODUCER_ARGS,
      '--runtime-lane', 'totally_invalid_lane',
      '--validate',
    ])
    expect(result.exitCode).toBe(1)
    expect(result.stderr).toMatch(/Invalid --runtime-lane/)
  })

  it('GIVEN no runtime_lane/completion_outcome/completion_source/hook_event flags WHEN the producer runs THEN it stays backward compatible (fields absent, still schema-valid)', () => {
    const result = runProducer([...BASE_PRODUCER_ARGS, '--validate'])
    expect(result.exitCode).toBe(0)
    const manifest = JSON.parse(result.stdout)
    expect(manifest.runtime_lane).toBeUndefined()
    expect(manifest.completion_outcome).toBeUndefined()
    expect(manifest.completion_source).toBeUndefined()
    expect(manifest.hook_event).toBeUndefined()
  })
})

describe('schema enum coverage (#2489 AC1/AC2/AC3)', () => {
  const schema = JSON.parse(readFileSync(SCHEMA_PATH, 'utf-8'))

  it('GIVEN the schema WHEN inspected THEN runtime_lane enum includes native_claude_code/claude_gpt/codex_cli/unknown', () => {
    expect(schema.properties.runtime_lane.enum).toEqual(['native_claude_code', 'claude_gpt', 'codex_cli', 'unknown'])
  })

  it('GIVEN the schema WHEN inspected THEN completion_outcome/completion_source enums match the reframed taxonomy (no single stop_reason enum)', () => {
    expect(schema.properties.completion_outcome.enum).toEqual(['completed', 'failed', 'interrupted', 'incomplete', 'unavailable'])
    expect(schema.properties.completion_source.enum).toEqual(['hook', 'launcher', 'reconciliation', 'unavailable'])
  })

  it('GIVEN the schema WHEN inspected THEN runtime_lane/completion_outcome/completion_source are not in the root required array (backward compatibility)', () => {
    expect(schema.required).not.toContain('runtime_lane')
    expect(schema.required).not.toContain('completion_outcome')
    expect(schema.required).not.toContain('completion_source')
  })

  it('GIVEN the schema WHEN inspected THEN completion_outcome/completion_source form a dependentRequired pair (#2489 P2)', () => {
    expect(schema.dependentRequired.completion_outcome).toEqual(['completion_source'])
    expect(schema.dependentRequired.completion_source).toEqual(['completion_outcome'])
  })

  it('GIVEN the schema WHEN inspected THEN hook_event.event_type includes StopFailure alongside the existing events', () => {
    expect(schema.properties.hook_event.properties.event_type.enum).toContain('StopFailure')
    expect(schema.properties.hook_event.properties.event_type.enum).toContain('Stop')
  })

  it('GIVEN the schema WHEN inspected THEN hook_event.error_type enum matches the upstream StopFailure structured error taxonomy', () => {
    expect(schema.properties.hook_event.properties.error_type.enum).toEqual([
      'rate_limit',
      'overloaded',
      'authentication_failed',
      'billing_error',
      'invalid_request',
      'model_not_found',
      'server_error',
      'max_output_tokens',
      'unknown',
    ])
  })
})
