/**
 * runtime-lane-completion-hook-wiring.test.ts
 *
 * #2489 AC1 / AC2 / AC3 (#1939 AC4 相当):
 *
 * - AC1: runtime_lane の provenance は launcher-set 環境変数
 *   CLAUDE_GPT_CLAUDE_BIN の有無のみに由来し、actor.name や transcript から
 *   推測しないことを確認する。
 * - AC2: Stop -> completed/hook, StopFailure -> failed/hook を hook イベント
 *   名からのみ導出し、それ以外のイベントは推測せず null/null を返すことを
 *   確認する。
 * - AC1/AC2: buildProducerArgs が --runtime-lane / --completion-outcome /
 *   --completion-source を条件付きで含めることを確認する。
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
  resolveCompletionFields,
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

describe('resolveCompletionFields (#2489 AC2) — derived from hook_event_name only, never inferred', () => {
  it('GIVEN Stop WHEN resolved THEN completion_outcome=completed / completion_source=hook', () => {
    expect(resolveCompletionFields('Stop')).toEqual({ completionOutcome: 'completed', completionSource: 'hook' })
  })

  it('GIVEN StopFailure WHEN resolved THEN completion_outcome=failed / completion_source=hook', () => {
    expect(resolveCompletionFields('StopFailure')).toEqual({ completionOutcome: 'failed', completionSource: 'hook' })
  })

  it('GIVEN an event with no observed terminal signal in this hook path WHEN resolved THEN both fields are null (not guessed)', () => {
    expect(resolveCompletionFields('PostToolUse')).toEqual({ completionOutcome: null, completionSource: null })
    expect(resolveCompletionFields('SubagentStop')).toEqual({ completionOutcome: null, completionSource: null })
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

  it('GIVEN runtimeLane/completionOutcome/completionSource provided WHEN built THEN all three CLI flags are included', () => {
    const args = buildProducerArgs({
      ...baseParams,
      runtimeLane: 'claude_gpt',
      completionOutcome: 'failed',
      completionSource: 'hook',
    })
    expect(args[args.indexOf('--runtime-lane') + 1]).toBe('claude_gpt')
    expect(args[args.indexOf('--completion-outcome') + 1]).toBe('failed')
    expect(args[args.indexOf('--completion-source') + 1]).toBe('hook')
  })

  it('GIVEN runtimeLane/completionOutcome/completionSource are null (no defaults provided) WHEN built THEN none of the three flags are included', () => {
    const args = buildProducerArgs({ ...baseParams })
    expect(args).not.toContain('--runtime-lane')
    expect(args).not.toContain('--completion-outcome')
    expect(args).not.toContain('--completion-source')
  })
})

describe('producer CLI round-trip (#2489 AC1/AC2/AC3) — schema validator accepts the new optional fields', () => {
  it('GIVEN --runtime-lane/--completion-outcome/--completion-source WHEN the producer runs THEN it emits a schema-valid manifest with those fields set', () => {
    const result = runProducer([
      ...BASE_PRODUCER_ARGS,
      '--runtime-lane', 'native_claude_code',
      '--completion-outcome', 'completed',
      '--completion-source', 'hook',
      '--validate',
    ])
    expect(result.exitCode).toBe(0)
    const manifest = JSON.parse(result.stdout)
    expect(manifest.runtime_lane).toBe('native_claude_code')
    expect(manifest.completion_outcome).toBe('completed')
    expect(manifest.completion_source).toBe('hook')
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

  it('GIVEN no runtime_lane/completion_outcome/completion_source flags WHEN the producer runs THEN it stays backward compatible (fields absent, still schema-valid)', () => {
    const result = runProducer([...BASE_PRODUCER_ARGS, '--validate'])
    expect(result.exitCode).toBe(0)
    const manifest = JSON.parse(result.stdout)
    expect(manifest.runtime_lane).toBeUndefined()
    expect(manifest.completion_outcome).toBeUndefined()
    expect(manifest.completion_source).toBeUndefined()
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

  it('GIVEN the schema WHEN inspected THEN hook_event.event_type includes StopFailure alongside the existing events', () => {
    expect(schema.properties.hook_event.properties.event_type.enum).toContain('StopFailure')
    expect(schema.properties.hook_event.properties.event_type.enum).toContain('Stop')
  })
})
