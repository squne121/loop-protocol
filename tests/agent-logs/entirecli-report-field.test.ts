/**
 * AC8: checker の verdict が agent_run_report.public_safety.entirecli_safety に取り込める
 *
 * AC7 Stop Condition: docs/schemas/agent-run-report.schema.json に entirecli_safety フィールドが
 * 存在しないため、schema 追加は scope 外。本テストは verdict 計算の結果オブジェクトが
 * report フィールドとして保持できる構造を持つことを検証する（schema 統合は別 follow-up Issue）。
 */
import { describe, expect, it } from 'vitest'
import { checkEntireCLISafety, SCHEMA_VERSION } from '../../scripts/agent-logs/lib/entirecli-safety.mjs'

function makeNotApplicableInput() {
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

function makeSafeInput() {
  return {
    entireBinaryPresent: true,
    entireDirPresent: false,
    entireHooksPresent: false,
    localRefs: [],
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
  }
}

function makeBlockedInput() {
  return {
    ...makeSafeInput(),
    baseSettings: {
      strategy_options: { push_sessions: true, telemetry: false },
    },
  }
}

describe('entirecli-report-field', () => {
  describe('result structure compatibility', () => {
    it('GIVEN not_applicable result WHEN embedded in report-like object THEN all fields present', () => {
      const safetyResult = checkEntireCLISafety(makeNotApplicableInput())

      // Simulate embedding in a report-like structure
      const reportLike = {
        public_safety: {
          entirecli_safety: safetyResult,
        },
      }

      expect(reportLike.public_safety.entirecli_safety.schema_version).toBe(SCHEMA_VERSION)
      expect(reportLike.public_safety.entirecli_safety.verdict).toBe('not_applicable')
      expect(reportLike.public_safety.entirecli_safety.reason_codes).toBeInstanceOf(Array)
      expect(typeof reportLike.public_safety.entirecli_safety.raw_values_emitted).toBe('boolean')
    })

    it('GIVEN safe result WHEN embedded in report-like object THEN verdict is safe', () => {
      const safetyResult = checkEntireCLISafety(makeSafeInput())

      const reportLike = {
        public_safety: {
          entirecli_safety: safetyResult,
        },
      }

      expect(reportLike.public_safety.entirecli_safety.verdict).toBe('safe')
    })

    it('GIVEN blocked result WHEN embedded in report-like object THEN verdict is blocked with reason_codes', () => {
      const safetyResult = checkEntireCLISafety(makeBlockedInput())

      const reportLike = {
        public_safety: {
          entirecli_safety: safetyResult,
        },
      }

      expect(reportLike.public_safety.entirecli_safety.verdict).toBe('blocked')
      expect(reportLike.public_safety.entirecli_safety.reason_codes.length).toBeGreaterThan(0)
    })
  })

  describe('result schema fields', () => {
    it('GIVEN any verdict WHEN checked THEN schema_version is entirecli_safety_result/v1', () => {
      for (const input of [makeNotApplicableInput(), makeSafeInput(), makeBlockedInput()]) {
        const result = checkEntireCLISafety(input)
        expect(result.schema_version).toBe('entirecli_safety_result/v1')
      }
    })

    it('GIVEN any verdict WHEN checked THEN verdict is one of not_applicable/safe/blocked', () => {
      const verdicts = new Set(['not_applicable', 'safe', 'blocked'])
      for (const input of [makeNotApplicableInput(), makeSafeInput(), makeBlockedInput()]) {
        const result = checkEntireCLISafety(input)
        expect(verdicts.has(result.verdict)).toBe(true)
      }
    })

    it('GIVEN any verdict WHEN checked THEN reason_codes is an array', () => {
      for (const input of [makeNotApplicableInput(), makeSafeInput(), makeBlockedInput()]) {
        const result = checkEntireCLISafety(input)
        expect(result.reason_codes).toBeInstanceOf(Array)
      }
    })

    it('GIVEN any verdict WHEN checked THEN raw_values_emitted is a boolean', () => {
      for (const input of [makeNotApplicableInput(), makeSafeInput(), makeBlockedInput()]) {
        const result = checkEntireCLISafety(input)
        expect(typeof result.raw_values_emitted).toBe('boolean')
      }
    })

    it('GIVEN safe verdict WHEN checked THEN raw_values_emitted is false', () => {
      const result = checkEntireCLISafety(makeSafeInput())
      expect(result.raw_values_emitted).toBe(false)
    })

    it('GIVEN not_applicable verdict WHEN checked THEN reason_codes contains entire_absent', () => {
      const result = checkEntireCLISafety(makeNotApplicableInput())
      expect(result.reason_codes).toContain('entire_absent')
    })
  })

  describe('AC7 scope boundary', () => {
    it('GIVEN entirecli_safety result WHEN injected into public_safety field THEN no schema validation occurs (schema not in scope)', () => {
      // This test documents the AC7 Stop Condition:
      // The schema at docs/schemas/agent-run-report.schema.json does not have
      // entirecli_safety field, so schema integration is a separate follow-up.
      // This test verifies the result object is structurally compatible.
      const result = checkEntireCLISafety(makeSafeInput())

      expect(result).toMatchObject({
        schema_version: expect.stringContaining('entirecli_safety_result'),
        verdict: expect.stringMatching(/^(not_applicable|safe|blocked)$/),
        reason_codes: expect.any(Array),
        raw_values_emitted: expect.any(Boolean),
      })
    })
  })
})
