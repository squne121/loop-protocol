import { describe, expect, it } from 'vitest'

import { createValidReport } from './report-test-fixtures'
import { validateReportAgainstSchema } from '../../scripts/lib/agent-run-report-validation.mjs'
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
    codeRemoteVisibility: 'local_only' as const,
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
    codeRemoteVisibility: 'local_only' as const,
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

function createReportWithEntirecliSafety(entirecliSafety: Record<string, unknown>) {
  return {
    ...createValidReport(),
    public_safety: {
      ...createValidReport().public_safety,
      entirecli_safety: entirecliSafety,
    },
  }
}

describe('entirecli-report-field', () => {
  describe('result structure compatibility', () => {
    it('GIVEN any checker result WHEN embedded in report THEN schema_version is fixed to EntireCLISafetyResult/v1', () => {
      for (const input of [makeNotApplicableInput(), makeSafeInput(), makeBlockedInput()]) {
        const result = checkEntireCLISafety(input)
        expect(result.schema_version).toBe(SCHEMA_VERSION)
      }
    })

    it('GIVEN a safe checker result WHEN embedded in a public report THEN schema admission passes', () => {
      const result = checkEntireCLISafety(makeSafeInput())
      const validation = validateReportAgainstSchema(createReportWithEntirecliSafety(result))

      expect(result.verdict).toBe('safe')
      expect(validation.valid).toBe(true)
    })

    it('GIVEN a not_applicable checker result WHEN embedded in a public report THEN schema admission passes', () => {
      const result = checkEntireCLISafety(makeNotApplicableInput())
      const validation = validateReportAgainstSchema(createReportWithEntirecliSafety(result))

      expect(result.verdict).toBe('not_applicable')
      expect(result.reason_codes).toEqual(['entire_absent'])
      expect(validation.valid).toBe(true)
    })

    it('GIVEN a blocked checker result WHEN embedded in a public report THEN schema admission fails closed', () => {
      const result = checkEntireCLISafety(makeBlockedInput())
      const validation = validateReportAgainstSchema(createReportWithEntirecliSafety(result))

      expect(result.verdict).toBe('blocked')
      expect(result.reason_codes.length).toBeGreaterThan(0)
      expect(validation.valid).toBe(false)
    })
  })

  describe('public report invariants', () => {
    it('GIVEN a safe verdict WHEN reason_codes is not empty THEN schema admission fails', () => {
      const validation = validateReportAgainstSchema(createReportWithEntirecliSafety({
        ...checkEntireCLISafety(makeSafeInput()),
        reason_codes: ['push_sessions_enabled'],
      }))

      expect(validation.valid).toBe(false)
    })

    it('GIVEN a public report WHEN raw_values_emitted is true THEN schema admission fails', () => {
      const validation = validateReportAgainstSchema(createReportWithEntirecliSafety({
        ...checkEntireCLISafety(makeSafeInput()),
        verdict: 'blocked',
        reason_codes: ['raw_value_redaction_violation'],
        raw_values_emitted: true,
      }))

      expect(validation.valid).toBe(false)
    })

    it('GIVEN a not_applicable verdict WHEN reason_codes omits entire_absent THEN schema admission fails', () => {
      const validation = validateReportAgainstSchema(createReportWithEntirecliSafety({
        ...checkEntireCLISafety(makeNotApplicableInput()),
        reason_codes: [],
      }))

      expect(validation.valid).toBe(false)
    })
  })
})
