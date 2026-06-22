/**
 * AC1: EntireCLI 未使用時は not_applicable verdict を出す
 */
import { describe, expect, it } from 'vitest'
import { checkEntireCLISafety, ReasonCode, SCHEMA_VERSION } from '../../scripts/agent-logs/lib/entirecli-safety.mjs'

function makeAbsentInput() {
  return {
    entireBinaryPresent: false,
    entireDirPresent: false,
    entireHooksPresent: false,
    localRefs: [],
    checkpointTrailerPresent: false,
    tokenEnvPresent: false,
    baseSettings: {},
    localSettings: {},
    checkpointRemote: null,
    checkpointRemoteVisibility: 'unknown' as const,
    remoteBranches: [],
    gitConfig: {},
    gitConfigParseErrors: [],
    diagnosticStrings: [],
  }
}

describe('entirecli-not-applicable', () => {
  it('GIVEN no entire binary, dir, hooks, refs, token, or settings WHEN checked THEN verdict is not_applicable', () => {
    const result = checkEntireCLISafety(makeAbsentInput())

    expect(result.schema_version).toBe(SCHEMA_VERSION)
    expect(result.verdict).toBe('not_applicable')
    expect(result.reason_codes).toContain(ReasonCode.ENTIRE_ABSENT)
    expect(result.raw_values_emitted).toBe(false)
  })

  it('GIVEN empty localRefs array with no checkpoint refs WHEN checked THEN verdict is not_applicable', () => {
    const result = checkEntireCLISafety({
      ...makeAbsentInput(),
      localRefs: ['refs/heads/main', 'refs/heads/feature/my-feature', 'refs/remotes/origin/main'],
    })

    expect(result.verdict).toBe('not_applicable')
  })

  it('GIVEN unrelated remote branches WHEN checked THEN verdict is not_applicable', () => {
    const result = checkEntireCLISafety({
      ...makeAbsentInput(),
      remoteBranches: ['origin/main', 'origin/develop', 'origin/feature/something'],
    })

    expect(result.verdict).toBe('not_applicable')
  })

  it('GIVEN non-empty git config but no entire indicators WHEN checked THEN verdict is not_applicable', () => {
    const result = checkEntireCLISafety({
      ...makeAbsentInput(),
      gitConfig: {
        'user.email': 'test@example.com',
        'core.autocrlf': 'false',
        'remote.origin.url': 'https://github.com/example/repo.git',
      },
    })

    expect(result.verdict).toBe('not_applicable')
  })

  it('GIVEN entire binary present WHEN checked THEN verdict is NOT not_applicable', () => {
    const result = checkEntireCLISafety({
      ...makeAbsentInput(),
      entireBinaryPresent: true,
    })

    expect(result.verdict).not.toBe('not_applicable')
  })

  it('GIVEN entire dir present WHEN checked THEN verdict is NOT not_applicable', () => {
    const result = checkEntireCLISafety({
      ...makeAbsentInput(),
      entireDirPresent: true,
    })

    expect(result.verdict).not.toBe('not_applicable')
  })

  it('GIVEN token env present WHEN checked THEN verdict is NOT not_applicable', () => {
    const result = checkEntireCLISafety({
      ...makeAbsentInput(),
      tokenEnvPresent: true,
    })

    expect(result.verdict).not.toBe('not_applicable')
  })

  it('GIVEN checkpoint_remote configured WHEN checked THEN verdict is NOT not_applicable', () => {
    const result = checkEntireCLISafety({
      ...makeAbsentInput(),
      checkpointRemote: 'origin',
    })

    expect(result.verdict).not.toBe('not_applicable')
  })

  it('GIVEN base settings present WHEN checked THEN verdict is NOT not_applicable', () => {
    const result = checkEntireCLISafety({
      ...makeAbsentInput(),
      baseSettings: { strategy_options: { push_sessions: false, telemetry: false } },
    })

    expect(result.verdict).not.toBe('not_applicable')
  })

  it('GIVEN checkpoint trailer in commits WHEN checked THEN verdict is NOT not_applicable', () => {
    const result = checkEntireCLISafety({
      ...makeAbsentInput(),
      checkpointTrailerPresent: true,
    })

    expect(result.verdict).not.toBe('not_applicable')
  })
})
