/**
 * AC4: entire/checkpoints/v1, entire/*, *checkpoint*, *session* remote branch を検査する
 */
import { describe, expect, it } from 'vitest'
import { checkEntireCLISafety, isCheckpointBranch, ReasonCode } from '../../scripts/agent-logs/lib/entirecli-safety.mjs'

function makeSafeInputWithBranch(remoteBranches: string[]) {
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
    remoteBranches,
    gitConfig: {},
    gitConfigParseErrors: [],
    diagnosticStrings: [],
  }
}

describe('entirecli-remote-branches', () => {
  describe('isCheckpointBranch pattern matching', () => {
    it('GIVEN entire/checkpoints/v1 branch WHEN tested THEN returns true', () => {
      expect(isCheckpointBranch('entire/checkpoints/v1')).toBe(true)
    })

    it('GIVEN entire/checkpoints/v1/run-123 branch WHEN tested THEN returns true', () => {
      expect(isCheckpointBranch('entire/checkpoints/v1/run-123')).toBe(true)
    })

    it('GIVEN entire/anything branch WHEN tested THEN returns true', () => {
      expect(isCheckpointBranch('entire/sessions')).toBe(true)
      expect(isCheckpointBranch('entire/my-branch')).toBe(true)
    })

    it('GIVEN branch with checkpoint in name WHEN tested THEN returns true', () => {
      expect(isCheckpointBranch('feature/checkpoint-backup')).toBe(true)
      expect(isCheckpointBranch('my-checkpoint')).toBe(true)
    })

    it('GIVEN branch with session in name WHEN tested THEN returns true', () => {
      expect(isCheckpointBranch('feature/session-data')).toBe(true)
      expect(isCheckpointBranch('my-session')).toBe(true)
    })

    it('GIVEN normal branch name WHEN tested THEN returns false', () => {
      expect(isCheckpointBranch('main')).toBe(false)
      expect(isCheckpointBranch('feature/my-feature')).toBe(false)
      expect(isCheckpointBranch('develop')).toBe(false)
      expect(isCheckpointBranch('origin/main')).toBe(false)
    })
  })

  describe('remote branch safety check', () => {
    it('GIVEN entire/checkpoints/v1 remote branch WHEN checked THEN verdict is blocked', () => {
      const result = checkEntireCLISafety(makeSafeInputWithBranch(['origin/entire/checkpoints/v1']))

      expect(result.verdict).toBe('blocked')
      expect(result.reason_codes).toContain(ReasonCode.PUBLIC_CHECKPOINT_BRANCH_PRESENT)
    })

    it('GIVEN entire/* remote branch WHEN checked THEN verdict is blocked', () => {
      const result = checkEntireCLISafety(makeSafeInputWithBranch(['origin/entire/sessions']))

      expect(result.verdict).toBe('blocked')
      expect(result.reason_codes).toContain(ReasonCode.PUBLIC_CHECKPOINT_BRANCH_PRESENT)
    })

    it('GIVEN *checkpoint* remote branch WHEN checked THEN verdict is blocked', () => {
      const result = checkEntireCLISafety(makeSafeInputWithBranch(['origin/feature/checkpoint-sync']))

      expect(result.verdict).toBe('blocked')
      expect(result.reason_codes).toContain(ReasonCode.PUBLIC_CHECKPOINT_BRANCH_PRESENT)
    })

    it('GIVEN *session* remote branch WHEN checked THEN verdict is blocked', () => {
      const result = checkEntireCLISafety(makeSafeInputWithBranch(['origin/session-backup-2026']))

      expect(result.verdict).toBe('blocked')
      expect(result.reason_codes).toContain(ReasonCode.PUBLIC_CHECKPOINT_BRANCH_PRESENT)
    })

    it('GIVEN only safe remote branches WHEN checked THEN no checkpoint branch reason code', () => {
      const result = checkEntireCLISafety(makeSafeInputWithBranch([
        'origin/main',
        'origin/develop',
        'origin/feature/add-feature',
      ]))

      expect(result.reason_codes).not.toContain(ReasonCode.PUBLIC_CHECKPOINT_BRANCH_PRESENT)
    })

    it('GIVEN multiple branches with one checkpoint branch WHEN checked THEN blocked', () => {
      const result = checkEntireCLISafety(makeSafeInputWithBranch([
        'origin/main',
        'origin/entire/checkpoints/v1',
        'origin/develop',
      ]))

      expect(result.verdict).toBe('blocked')
      expect(result.reason_codes).toContain(ReasonCode.PUBLIC_CHECKPOINT_BRANCH_PRESENT)
    })

    it('GIVEN remote branches with SESSION uppercase WHEN checked THEN blocked (case-insensitive)', () => {
      const result = checkEntireCLISafety(makeSafeInputWithBranch(['origin/SESSION-BACKUP']))

      expect(result.verdict).toBe('blocked')
      expect(result.reason_codes).toContain(ReasonCode.PUBLIC_CHECKPOINT_BRANCH_PRESENT)
    })

    it('GIVEN local ref with entire/checkpoints/v1 WHEN checked THEN entire detected', () => {
      const result = checkEntireCLISafety({
        entireBinaryPresent: false,
        entireDirPresent: false,
        entireHooksPresent: false,
        localRefs: ['refs/heads/entire/checkpoints/v1'],
        checkpointTrailerPresent: false,
        tokenEnvPresent: false,
        baseSettings: { strategy_options: { push_sessions: false, telemetry: false } },
        localSettings: {},
        checkpointRemote: null,
        checkpointRemoteVisibility: 'local_only' as const,
        codeRemoteVisibility: 'local_only' as const,
        remoteBranches: [],
        gitConfig: {},
        gitConfigParseErrors: [],
        diagnosticStrings: [],
      })

      // entire is detected via local ref — should not be not_applicable
      expect(result.verdict).not.toBe('not_applicable')
    })
  })
})
