/**
 * AC2: EntireCLI 使用時は available surface を記録する
 * fixture / mocked command runner で検証し、live EntireCLI binary は不要
 *
 * checked_surfaces フィールドに以下を記録する:
 *   - entire_binary: 存在有無
 *   - entire_version: バージョン文字列の redacted fingerprint またはエラー
 *   - entire_enable_help: entire enable --help の surface 有無
 *   - entire_configure_help: entire configure --help の surface 有無
 * raw output は出力しない（reason_code + redacted fingerprint のみ）
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
    entireVersion: 'entire 1.2.3',
    entireEnableHelp: 'Usage: entire enable [options]',
    entireConfigureHelp: 'Usage: entire configure [options]',
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
    codeRemoteVisibility: 'local_only' as const,
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

  describe('AC2: checked_surfaces recording', () => {
    it('GIVEN entire binary present WHEN checked THEN checked_surfaces.entire_binary is true', () => {
      const result = checkEntireCLISafety(makeEntireDetectedInput())

      expect(result.checked_surfaces.entire_binary).toBe(true)
    })

    it('GIVEN entire version string provided WHEN checked THEN checked_surfaces.entire_version is redacted fingerprint', () => {
      const result = checkEntireCLISafety(makeEntireDetectedInput({
        entireVersion: 'entire 1.2.3',
      }))

      // Must not emit raw version string
      expect(result.checked_surfaces.entire_version).not.toBe('entire 1.2.3')
      // Must contain redaction marker
      expect(result.checked_surfaces.entire_version).toContain('len=')
    })

    it('GIVEN entire enable --help surface available WHEN checked THEN checked_surfaces.entire_enable_help is true', () => {
      const result = checkEntireCLISafety(makeEntireDetectedInput({
        entireEnableHelp: 'Usage: entire enable [options]',
      }))

      expect(result.checked_surfaces.entire_enable_help).toBe(true)
    })

    it('GIVEN entire configure --help surface available WHEN checked THEN checked_surfaces.entire_configure_help is true', () => {
      const result = checkEntireCLISafety(makeEntireDetectedInput({
        entireConfigureHelp: 'Usage: entire configure [options]',
      }))

      expect(result.checked_surfaces.entire_configure_help).toBe(true)
    })

    it('GIVEN entire binary absent WHEN checked THEN checked_surfaces.entire_binary is false', () => {
      const result = checkEntireCLISafety(makeEntireDetectedInput({
        entireBinaryPresent: false,
        entireVersion: null,
        entireEnableHelp: null,
        entireConfigureHelp: null,
        entireDirPresent: true,  // Keep detected via dir
      }))

      expect(result.checked_surfaces.entire_binary).toBe(false)
    })

    it('GIVEN entire version null (binary absent) WHEN checked THEN checked_surfaces.entire_version is null', () => {
      const result = checkEntireCLISafety(makeEntireDetectedInput({
        entireBinaryPresent: false,
        entireVersion: null,
        entireEnableHelp: null,
        entireConfigureHelp: null,
        entireDirPresent: true,
      }))

      expect(result.checked_surfaces.entire_version).toBeNull()
    })

    it('GIVEN help surfaces unavailable WHEN checked THEN checked_surfaces reflects false for each', () => {
      const result = checkEntireCLISafety(makeEntireDetectedInput({
        entireEnableHelp: null,
        entireConfigureHelp: null,
      }))

      expect(result.checked_surfaces.entire_enable_help).toBe(false)
      expect(result.checked_surfaces.entire_configure_help).toBe(false)
    })

    it('GIVEN checked_surfaces WHEN raw output inspected THEN no raw version string emitted', () => {
      const rawVersion = 'entire 1.2.3-beta+build.456'
      const result = checkEntireCLISafety(makeEntireDetectedInput({
        entireVersion: rawVersion,
      }))

      const resultStr = JSON.stringify(result)
      expect(resultStr).not.toContain(rawVersion)
    })

    it('GIVEN not_applicable verdict WHEN checked THEN checked_surfaces is still present', () => {
      const result = checkEntireCLISafety({
        entireBinaryPresent: false,
        entireVersion: null,
        entireEnableHelp: null,
        entireConfigureHelp: null,
        entireDirPresent: false,
        entireHooksPresent: false,
        localRefs: [],
        checkpointTrailerPresent: false,
        tokenEnvPresent: false,
        baseSettings: {},
        localSettings: {},
        checkpointRemote: null,
        checkpointRemoteVisibility: 'unknown' as const,
        codeRemoteVisibility: 'local_only' as const,
        remoteBranches: [],
        gitConfig: {},
        gitConfigParseErrors: [],
        diagnosticStrings: [],
      })

      expect(result.verdict).toBe('not_applicable')
      expect(result.checked_surfaces).toBeDefined()
      expect(result.checked_surfaces.entire_binary).toBe(false)
    })
  })

  it('GIVEN entire dir present (binary absent) with safe settings WHEN checked THEN entire detected', () => {
    const result = checkEntireCLISafety(makeEntireDetectedInput({
      entireBinaryPresent: false,
      entireVersion: null,
      entireEnableHelp: null,
      entireConfigureHelp: null,
      entireDirPresent: true,
    }))

    expect(result.verdict).not.toBe('not_applicable')
  })

  it('GIVEN entire hooks present WHEN checked THEN entire detected (surface recorded)', () => {
    const result = checkEntireCLISafety(makeEntireDetectedInput({
      entireBinaryPresent: false,
      entireVersion: null,
      entireEnableHelp: null,
      entireConfigureHelp: null,
      entireDirPresent: false,
      entireHooksPresent: true,
    }))

    expect(result.verdict).not.toBe('not_applicable')
  })

  it('GIVEN local ref contains entire/checkpoints/v1 WHEN checked THEN entire presence detected', () => {
    const result = checkEntireCLISafety({
      entireBinaryPresent: false,
      entireVersion: null,
      entireEnableHelp: null,
      entireConfigureHelp: null,
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
      codeRemoteVisibility: 'local_only' as const,
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
