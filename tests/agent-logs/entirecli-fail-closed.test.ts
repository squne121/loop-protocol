/**
 * AC3: safe verdict は以下をすべて満たす場合のみ:
 * - push_sessions: false (effective config)
 * - telemetry: false (未設定は blocked)
 * - checkpoint_remote が private_verified または local-only
 * - ENTIRE_CHECKPOINT_TOKEN 存在時は checkpoint_remote が private_verified
 * - public/unknown/non-GitHub/parse error/network error/auth error はすべて blocked
 */
import { describe, expect, it } from 'vitest'
import { checkEntireCLISafety, ReasonCode } from '../../scripts/agent-logs/lib/entirecli-safety.mjs'

function makeSafeInput() {
  return {
    entireBinaryPresent: true,
    entireDirPresent: false,
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
  }
}

describe('entirecli-fail-closed', () => {
  it('GIVEN push_sessions true WHEN checked THEN verdict is blocked with push_sessions_enabled', () => {
    const result = checkEntireCLISafety({
      ...makeSafeInput(),
      baseSettings: { strategy_options: { push_sessions: true, telemetry: false } },
    })

    expect(result.verdict).toBe('blocked')
    expect(result.reason_codes).toContain(ReasonCode.PUSH_SESSIONS_ENABLED)
  })

  it('GIVEN push_sessions not configured (null) WHEN checked THEN verdict is blocked with push_sessions_unknown', () => {
    const result = checkEntireCLISafety({
      ...makeSafeInput(),
      baseSettings: { strategy_options: { telemetry: false } },
    })

    expect(result.verdict).toBe('blocked')
    expect(result.reason_codes).toContain(ReasonCode.PUSH_SESSIONS_UNKNOWN)
  })

  it('GIVEN telemetry true WHEN checked THEN verdict is blocked with telemetry_enabled', () => {
    const result = checkEntireCLISafety({
      ...makeSafeInput(),
      baseSettings: { strategy_options: { push_sessions: false, telemetry: true } },
    })

    expect(result.verdict).toBe('blocked')
    expect(result.reason_codes).toContain(ReasonCode.TELEMETRY_ENABLED)
  })

  it('GIVEN telemetry not configured WHEN checked THEN verdict is blocked with telemetry_unknown', () => {
    const result = checkEntireCLISafety({
      ...makeSafeInput(),
      baseSettings: { strategy_options: { push_sessions: false } },
    })

    expect(result.verdict).toBe('blocked')
    expect(result.reason_codes).toContain(ReasonCode.TELEMETRY_UNKNOWN)
  })

  it('GIVEN checkpoint_remote with unknown visibility WHEN checked THEN verdict is blocked', () => {
    const result = checkEntireCLISafety({
      ...makeSafeInput(),
      checkpointRemote: 'origin',
      checkpointRemoteVisibility: 'unknown',
    })

    expect(result.verdict).toBe('blocked')
    expect(result.reason_codes).toContain(ReasonCode.CHECKPOINT_REMOTE_UNKNOWN_VISIBILITY)
  })

  it('GIVEN checkpoint_remote with public visibility WHEN checked THEN verdict is blocked', () => {
    const result = checkEntireCLISafety({
      ...makeSafeInput(),
      checkpointRemote: 'origin',
      checkpointRemoteVisibility: 'public',
    })

    expect(result.verdict).toBe('blocked')
    expect(result.reason_codes).toContain(ReasonCode.CHECKPOINT_REMOTE_PUBLIC)
  })

  it('GIVEN checkpoint_remote with not_github WHEN checked THEN verdict is blocked', () => {
    const result = checkEntireCLISafety({
      ...makeSafeInput(),
      checkpointRemote: 'upstream',
      checkpointRemoteVisibility: 'not_github',
    })

    expect(result.verdict).toBe('blocked')
    expect(result.reason_codes).toContain(ReasonCode.CHECKPOINT_REMOTE_UNKNOWN_VISIBILITY)
  })

  it('GIVEN token present and no checkpoint_remote WHEN checked THEN blocked with token_without_private_remote', () => {
    const result = checkEntireCLISafety({
      ...makeSafeInput(),
      tokenEnvPresent: true,
      checkpointRemote: null,
    })

    expect(result.verdict).toBe('blocked')
    expect(result.reason_codes).toContain(ReasonCode.CHECKPOINT_TOKEN_WITHOUT_PRIVATE_REMOTE)
  })

  it('GIVEN token present and checkpoint_remote with unknown visibility WHEN checked THEN blocked', () => {
    const result = checkEntireCLISafety({
      ...makeSafeInput(),
      tokenEnvPresent: true,
      checkpointRemote: 'origin',
      checkpointRemoteVisibility: 'unknown',
    })

    expect(result.verdict).toBe('blocked')
    expect(result.reason_codes).toContain(ReasonCode.CHECKPOINT_TOKEN_WITHOUT_PRIVATE_REMOTE)
  })

  it('GIVEN token present and checkpoint_remote private WHEN checked THEN token_without_private_remote NOT in reasons', () => {
    const result = checkEntireCLISafety({
      ...makeSafeInput(),
      tokenEnvPresent: true,
      checkpointRemote: 'origin',
      checkpointRemoteVisibility: 'private',
    })

    expect(result.reason_codes).not.toContain(ReasonCode.CHECKPOINT_TOKEN_WITHOUT_PRIVATE_REMOTE)
  })

  it('GIVEN git config parse error WHEN checked THEN verdict is blocked', () => {
    const result = checkEntireCLISafety({
      ...makeSafeInput(),
      gitConfigParseErrors: ['git config --list failed'],
    })

    expect(result.verdict).toBe('blocked')
    expect(result.reason_codes).toContain(ReasonCode.GIT_CONFIG_PARSE_ERROR)
  })

  it('GIVEN raw secret in diagnostics WHEN checked THEN verdict is blocked with redaction_violation', () => {
    const result = checkEntireCLISafety({
      ...makeSafeInput(),
      diagnosticStrings: ['ghp_ABCDEFGHIJKLMNOPQRSTUVWX'],
    })

    expect(result.verdict).toBe('blocked')
    expect(result.reason_codes).toContain(ReasonCode.RAW_VALUE_REDACTION_VIOLATION)
    expect(result.raw_values_emitted).toBe(true)
  })

  it('GIVEN all safe conditions met WHEN checked THEN verdict is safe with empty reason_codes', () => {
    const result = checkEntireCLISafety(makeSafeInput())

    expect(result.verdict).toBe('safe')
    expect(result.reason_codes).toHaveLength(0)
    expect(result.raw_values_emitted).toBe(false)
  })
})
