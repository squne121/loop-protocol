/**
 * AC6: public repo への checkpoint / session push 経路が 1 つでも見つかれば blocked
 * unknown visibility も fail closed
 */
import { describe, expect, it } from 'vitest'
import { checkEntireCLISafety, ReasonCode } from '../../scripts/agent-logs/lib/entirecli-safety.mjs'

function makeSafeBase() {
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
    gitConfig: {} as Record<string, string[]>,
    gitConfigParseErrors: [],
    diagnosticStrings: [],
  }
}

describe('entirecli-blocked', () => {
  describe('public remote → blocked', () => {
    it('GIVEN checkpoint_remote with public visibility WHEN checked THEN blocked', () => {
      const result = checkEntireCLISafety({
        ...makeSafeBase(),
        checkpointRemote: 'origin',
        checkpointRemoteVisibility: 'public',
      })

      expect(result.verdict).toBe('blocked')
      expect(result.reason_codes).toContain(ReasonCode.CHECKPOINT_REMOTE_PUBLIC)
    })

    it('GIVEN public checkpoint branch in remote WHEN checked THEN blocked', () => {
      const result = checkEntireCLISafety({
        ...makeSafeBase(),
        remoteBranches: ['origin/entire/checkpoints/v1/run-001'],
      })

      expect(result.verdict).toBe('blocked')
      expect(result.reason_codes).toContain(ReasonCode.PUBLIC_CHECKPOINT_BRANCH_PRESENT)
    })

    it('GIVEN pushurl rewriting to non-GitHub HTTP WHEN checked THEN blocked', () => {
      const result = checkEntireCLISafety({
        ...makeSafeBase(),
        gitConfig: {
          'remote.origin.pushurl': ['http://public.example.com/checkpoints.git'],
        },
      })

      expect(result.verdict).toBe('blocked')
      expect(result.reason_codes).toContain(ReasonCode.PUBLIC_PUSH_REMOTE_DETECTED)
    })
  })

  describe('unknown visibility → fail closed', () => {
    it('GIVEN checkpoint_remote with unknown visibility WHEN checked THEN blocked', () => {
      const result = checkEntireCLISafety({
        ...makeSafeBase(),
        checkpointRemote: 'origin',
        checkpointRemoteVisibility: 'unknown',
      })

      expect(result.verdict).toBe('blocked')
      expect(result.reason_codes).toContain(ReasonCode.CHECKPOINT_REMOTE_UNKNOWN_VISIBILITY)
    })

    it('GIVEN non-GitHub remote as checkpoint_remote WHEN checked THEN blocked (unknown/not_github)', () => {
      const result = checkEntireCLISafety({
        ...makeSafeBase(),
        checkpointRemote: 'gitlab',
        checkpointRemoteVisibility: 'not_github',
      })

      expect(result.verdict).toBe('blocked')
      expect(result.reason_codes).toContain(ReasonCode.CHECKPOINT_REMOTE_UNKNOWN_VISIBILITY)
    })
  })

  describe('single push path → blocked', () => {
    it('GIVEN one blocked condition among otherwise safe config WHEN checked THEN blocked', () => {
      // Only one issue: push_sessions unknown
      const result = checkEntireCLISafety({
        ...makeSafeBase(),
        baseSettings: {
          strategy_options: {
            // push_sessions omitted → unknown
            telemetry: false,
          },
        },
      })

      expect(result.verdict).toBe('blocked')
      expect(result.reason_codes).toContain(ReasonCode.PUSH_SESSIONS_UNKNOWN)
    })

    it('GIVEN one session remote branch among otherwise safe branches WHEN checked THEN blocked', () => {
      const result = checkEntireCLISafety({
        ...makeSafeBase(),
        remoteBranches: [
          'origin/main',
          'origin/develop',
          'origin/session-state',  // this one triggers
        ],
      })

      expect(result.verdict).toBe('blocked')
      expect(result.reason_codes).toContain(ReasonCode.PUBLIC_CHECKPOINT_BRANCH_PRESENT)
    })
  })

  describe('multiple blocked conditions', () => {
    it('GIVEN push_sessions true AND checkpoint_remote public WHEN checked THEN reason_codes deduplicated', () => {
      const result = checkEntireCLISafety({
        ...makeSafeBase(),
        baseSettings: {
          strategy_options: { push_sessions: true, telemetry: false },
        },
        checkpointRemote: 'origin',
        checkpointRemoteVisibility: 'public',
      })

      expect(result.verdict).toBe('blocked')
      expect(result.reason_codes).toContain(ReasonCode.PUSH_SESSIONS_ENABLED)
      expect(result.reason_codes).toContain(ReasonCode.CHECKPOINT_REMOTE_PUBLIC)

      // Deduplication: no duplicate codes
      const unique = [...new Set(result.reason_codes)]
      expect(result.reason_codes).toEqual(unique)
    })
  })

  describe('safe conditions', () => {
    it('GIVEN all safe conditions AND no push paths WHEN checked THEN verdict is safe', () => {
      const result = checkEntireCLISafety(makeSafeBase())

      expect(result.verdict).toBe('safe')
      expect(result.reason_codes).toHaveLength(0)
    })

    it('GIVEN private checkpoint_remote WHEN checked THEN not blocked for visibility', () => {
      const result = checkEntireCLISafety({
        ...makeSafeBase(),
        checkpointRemote: 'origin',
        checkpointRemoteVisibility: 'private',
      })

      expect(result.reason_codes).not.toContain(ReasonCode.CHECKPOINT_REMOTE_PUBLIC)
      expect(result.reason_codes).not.toContain(ReasonCode.CHECKPOINT_REMOTE_UNKNOWN_VISIBILITY)
    })
  })
})
