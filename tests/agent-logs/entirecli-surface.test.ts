/**
 * AC2: EntireCLI 使用時は available surface を記録する
 * fixture / mocked command runner で検証し、live EntireCLI binary は不要
 */
import { describe, expect, it } from 'vitest'
import { checkEntireCLISafety, SCHEMA_VERSION } from '../../scripts/agent-logs/lib/entirecli-safety.mjs'

/**
 * Simulates a mocked command runner: entire binary present, surface info available.
 * No live binary required.
 */
function makeEntireDetectedInput(overrides: Record<string, unknown> = {}) {
  return {
    entireBinaryPresent: true,
    entireDirPresent: true,
    entireHooksPresent: false,
    localRefs: [],
    checkpointTrailerPresent: false,
    tokenEnvPresent: false,
    baseSettings: {
      strategy_options: {
        push_sessions: false,
        telemetry: false,
      },
    },
    localSettings: {},
    checkpointRemote: null,
    checkpointRemoteVisibility: 'local_only' as const,
    remoteBranches: [],
    gitConfig: {},
    gitConfigParseErrors: [],
    diagnosticStrings: [],
    ...overrides,
  }
}

describe('entirecli-surface', () => {
  it('GIVEN entire binary present with safe settings WHEN checked THEN verdict is safe (not not_applicable)', () => {
    const result = checkEntireCLISafety(makeEntireDetectedInput())

    expect(result.schema_version).toBe(SCHEMA_VERSION)
    expect(result.verdict).toBe('safe')
    expect(result.verdict).not.toBe('not_applicable')
  })

  it('GIVEN entire binary detected WHEN checked THEN schema_version is entirecli_safety_result/v1', () => {
    const result = checkEntireCLISafety(makeEntireDetectedInput())

    expect(result.schema_version).toBe('entirecli_safety_result/v1')
  })

  it('GIVEN entire dir present (binary absent) with safe settings WHEN checked THEN entire detected', () => {
    const result = checkEntireCLISafety(makeEntireDetectedInput({
      entireBinaryPresent: false,
      entireDirPresent: true,
    }))

    expect(result.verdict).not.toBe('not_applicable')
  })

  it('GIVEN entire hooks present WHEN checked THEN entire detected (surface recorded)', () => {
    const result = checkEntireCLISafety(makeEntireDetectedInput({
      entireBinaryPresent: false,
      entireDirPresent: false,
      entireHooksPresent: true,
    }))

    expect(result.verdict).not.toBe('not_applicable')
  })

  it('GIVEN local ref contains entire/checkpoints/v1 WHEN checked THEN entire presence detected', () => {
    const result = checkEntireCLISafety({
      entireBinaryPresent: false,
      entireDirPresent: false,
      entireHooksPresent: false,
      localRefs: ['refs/heads/entire/checkpoints/v1'],
      checkpointTrailerPresent: false,
      tokenEnvPresent: false,
      baseSettings: {
        strategy_options: { push_sessions: false, telemetry: false },
      },
      localSettings: {},
      checkpointRemote: null,
      checkpointRemoteVisibility: 'local_only' as const,
      remoteBranches: [],
      gitConfig: {},
      gitConfigParseErrors: [],
      diagnosticStrings: [],
    })

    expect(result.verdict).not.toBe('not_applicable')
  })

  it('GIVEN safe config WHEN checked THEN raw_values_emitted is false', () => {
    const result = checkEntireCLISafety(makeEntireDetectedInput())

    expect(result.raw_values_emitted).toBe(false)
  })

  it('GIVEN entire binary with push_sessions false and telemetry false WHEN checked THEN verdict safe', () => {
    const result = checkEntireCLISafety(makeEntireDetectedInput({
      baseSettings: { strategy_options: { push_sessions: false, telemetry: false } },
    }))

    expect(result.verdict).toBe('safe')
    expect(result.reason_codes).toHaveLength(0)
  })
})
