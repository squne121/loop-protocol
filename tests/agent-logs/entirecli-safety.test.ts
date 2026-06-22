/**
 * AC9: fixture で safe / public branch / pushurl rewrite / unknown visibility /
 * telemetry enabled / token present を検証し、env や git config の生値をそのまま出力しない
 */
import { describe, expect, it } from 'vitest'
import {
  checkEntireCLISafety,
  ReasonCode,
  redactFingerprint,
  containsRawValue,
  parseEntireSettings,
} from '../../scripts/agent-logs/lib/entirecli-safety.mjs'

// Fixture: safe configuration (all safety conditions met)
const FIXTURE_SAFE = {
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
      checkpoint_remote: 'origin',
    },
  },
  localSettings: {},
  checkpointRemote: 'origin',
  checkpointRemoteVisibility: 'private' as const,
  codeRemoteVisibility: 'local_only' as const,
  remoteBranches: ['origin/main', 'origin/develop'],
  gitConfig: {
    'remote.origin.url': ['https://github.com/user/private-repo.git'],
  } as Record<string, string[]>,
  gitConfigParseErrors: [],
  diagnosticStrings: [
    'checkpoint_remote: orig***[len=6]',
    'remote_url_fingerprint: http***[len=44]',
  ],
}

// Fixture: public checkpoint branch present
const FIXTURE_PUBLIC_BRANCH = {
  ...FIXTURE_SAFE,
  remoteBranches: [
    'origin/main',
    'origin/entire/checkpoints/v1',  // public checkpoint branch
  ],
}

// Fixture: pushurl rewrite to non-GitHub remote
const FIXTURE_PUSHURL_REWRITE = {
  ...FIXTURE_SAFE,
  gitConfig: {
    'remote.origin.url': ['https://github.com/user/private-repo.git'],
    'remote.origin.pushurl': ['http://internal.example.com/mirror/repo.git'],  // non-GitHub HTTP
  } as Record<string, string[]>,
}

// Fixture: unknown visibility (GitHub API 403/404)
const FIXTURE_UNKNOWN_VISIBILITY = {
  ...FIXTURE_SAFE,
  checkpointRemoteVisibility: 'unknown' as const,  // GitHub API auth error → unknown
  diagnosticStrings: [
    'checkpoint_remote: orig***[len=6]',
    'github_api: access_denied (status 403) — visibility unknown',
  ],
}

// Fixture: telemetry enabled
const FIXTURE_TELEMETRY_ENABLED = {
  ...FIXTURE_SAFE,
  baseSettings: {
    strategy_options: {
      push_sessions: false,
      telemetry: true,  // telemetry enabled → blocked
    },
  },
}

// Fixture: token present with private remote
const FIXTURE_TOKEN_PRESENT_PRIVATE = {
  ...FIXTURE_SAFE,
  tokenEnvPresent: true,
  checkpointRemoteVisibility: 'private' as const,
}

// Fixture: token present without private remote → blocked
const FIXTURE_TOKEN_PRESENT_NO_REMOTE = {
  ...FIXTURE_SAFE,
  tokenEnvPresent: true,
  checkpointRemote: null,  // no remote configured
  checkpointRemoteVisibility: 'unknown' as const,
}

// Fixture: includeIf with public pushInsteadOf (simulated via parse error since gitconfig file reading is not in scope)
const FIXTURE_INCLUDE_IF_PUBLIC_PUSH = {
  ...FIXTURE_SAFE,
  // Simulated: includeIf conditional block loaded, non-GitHub pushurl present
  gitConfig: {
    'remote.origin.url': ['https://github.com/user/private-repo.git'],
    'remote.origin.pushurl': ['http://public-mirror.io/repo.git'],  // non-GitHub HTTP pushurl
  } as Record<string, string[]>,
}

// Fixture: settings.local.json overrides settings.json (push enabled via local)
const FIXTURE_LOCAL_OVERRIDE_PUSH_ENABLED = {
  ...FIXTURE_SAFE,
  baseSettings: {
    strategy_options: {
      push_sessions: false,
      telemetry: false,
    },
  },
  localSettings: {
    strategy_options: {
      push_sessions: true,  // local override enables push → blocked
      telemetry: false,
    },
  },
}

describe('entirecli-safety (AC9 fixtures)', () => {
  describe('Fixture: safe', () => {
    it('GIVEN all safety conditions met WHEN checked THEN verdict is safe', () => {
      const result = checkEntireCLISafety(FIXTURE_SAFE)

      expect(result.verdict).toBe('safe')
      expect(result.reason_codes).toHaveLength(0)
      expect(result.raw_values_emitted).toBe(false)
    })

    it('GIVEN safe config WHEN checked THEN no raw secrets in result', () => {
      const result = checkEntireCLISafety(FIXTURE_SAFE)
      const resultStr = JSON.stringify(result)

      expect(containsRawValue(resultStr)).toBe(false)
    })

    it('GIVEN safe config WHEN checked THEN checked_surfaces is present', () => {
      const result = checkEntireCLISafety(FIXTURE_SAFE)

      expect(result.checked_surfaces).toBeDefined()
      expect(typeof result.checked_surfaces.entire_binary).toBe('boolean')
    })
  })

  describe('Fixture: public checkpoint branch', () => {
    it('GIVEN entire/checkpoints/v1 remote branch WHEN checked THEN verdict is blocked', () => {
      const result = checkEntireCLISafety(FIXTURE_PUBLIC_BRANCH)

      expect(result.verdict).toBe('blocked')
      expect(result.reason_codes).toContain(ReasonCode.PUBLIC_CHECKPOINT_BRANCH_PRESENT)
    })

    it('GIVEN blocked by public branch WHEN checked THEN raw_values_emitted is false', () => {
      const result = checkEntireCLISafety(FIXTURE_PUBLIC_BRANCH)

      expect(result.raw_values_emitted).toBe(false)
    })
  })

  describe('Fixture: pushurl rewrite', () => {
    it('GIVEN pushurl rewriting to non-GitHub HTTP WHEN checked THEN verdict is blocked', () => {
      const result = checkEntireCLISafety(FIXTURE_PUSHURL_REWRITE)

      expect(result.verdict).toBe('blocked')
      expect(result.reason_codes).toContain(ReasonCode.PUBLIC_PUSH_REMOTE_DETECTED)
    })
  })

  describe('Fixture: unknown visibility', () => {
    it('GIVEN GitHub API 403/404 resulting in unknown visibility WHEN checked THEN verdict is blocked', () => {
      const result = checkEntireCLISafety(FIXTURE_UNKNOWN_VISIBILITY)

      expect(result.verdict).toBe('blocked')
      expect(result.reason_codes).toContain(ReasonCode.CHECKPOINT_REMOTE_UNKNOWN_VISIBILITY)
    })
  })

  describe('Fixture: telemetry enabled', () => {
    it('GIVEN telemetry true in settings WHEN checked THEN verdict is blocked', () => {
      const result = checkEntireCLISafety(FIXTURE_TELEMETRY_ENABLED)

      expect(result.verdict).toBe('blocked')
      expect(result.reason_codes).toContain(ReasonCode.TELEMETRY_ENABLED)
    })
  })

  describe('Fixture: token present', () => {
    it('GIVEN token present and private remote WHEN checked THEN token does not cause block', () => {
      const result = checkEntireCLISafety(FIXTURE_TOKEN_PRESENT_PRIVATE)

      expect(result.reason_codes).not.toContain(ReasonCode.CHECKPOINT_TOKEN_WITHOUT_PRIVATE_REMOTE)
    })

    it('GIVEN token present and no checkpoint remote WHEN checked THEN blocked with token_without_private_remote', () => {
      const result = checkEntireCLISafety(FIXTURE_TOKEN_PRESENT_NO_REMOTE)

      expect(result.verdict).toBe('blocked')
      expect(result.reason_codes).toContain(ReasonCode.CHECKPOINT_TOKEN_WITHOUT_PRIVATE_REMOTE)
    })
  })

  describe('Fixture: includeIf public pushInsteadOf', () => {
    it('GIVEN includeIf loading public pushInsteadOf rewrite WHEN checked THEN blocked', () => {
      const result = checkEntireCLISafety(FIXTURE_INCLUDE_IF_PUBLIC_PUSH)

      expect(result.verdict).toBe('blocked')
      expect(result.reason_codes).toContain(ReasonCode.PUBLIC_PUSH_REMOTE_DETECTED)
    })
  })

  describe('Fixture: settings.local.json override push enabled', () => {
    it('GIVEN local settings override push_sessions to true WHEN checked THEN blocked', () => {
      const result = checkEntireCLISafety(FIXTURE_LOCAL_OVERRIDE_PUSH_ENABLED)

      expect(result.verdict).toBe('blocked')
      expect(result.reason_codes).toContain(ReasonCode.PUSH_SESSIONS_ENABLED)
    })

    it('GIVEN local settings override is applied WHEN parseEntireSettings called THEN local wins', () => {
      const { pushSessions } = parseEntireSettings(
        { strategy_options: { push_sessions: false, telemetry: false } },
        { strategy_options: { push_sessions: true, telemetry: false } }
      )

      expect(pushSessions).toBe(true)
    })
  })

  describe('Blocker 2: strategy_options.checkpoint_remote as object schema', () => {
    it('GIVEN checkpoint_remote as { provider, repo } object in strategy_options WHEN parseEntireSettings called THEN checkpointRemoteObj is object', () => {
      const { checkpointRemoteObj } = parseEntireSettings(
        {
          strategy_options: {
            push_sessions: false,
            telemetry: false,
            checkpoint_remote: { provider: 'github', repo: 'user/checkpoints' },
          },
        },
        {}
      )

      expect(checkpointRemoteObj).toEqual({ provider: 'github', repo: 'user/checkpoints' })
    })

    it('GIVEN checkpoint_remote as string in strategy_options WHEN parseEntireSettings called THEN checkpointRemoteObj is string', () => {
      const { checkpointRemoteObj } = parseEntireSettings(
        {
          strategy_options: {
            push_sessions: false,
            telemetry: false,
            checkpoint_remote: 'origin',
          },
        },
        {}
      )

      expect(checkpointRemoteObj).toBe('origin')
    })

    it('GIVEN top-level telemetry (official schema) WHEN parseEntireSettings called THEN telemetry read from top level', () => {
      const { telemetry } = parseEntireSettings(
        {
          telemetry: false,
          strategy_options: { push_sessions: false },
        },
        {}
      )

      expect(telemetry).toBe(false)
    })
  })

  describe('Redaction: no raw values in diagnostics', () => {
    it('GIVEN ghp_ token in diagnostics WHEN checked THEN blocked with redaction_violation', () => {
      const result = checkEntireCLISafety({
        ...FIXTURE_SAFE,
        diagnosticStrings: ['ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ12'],
      })

      expect(result.verdict).toBe('blocked')
      expect(result.reason_codes).toContain(ReasonCode.RAW_VALUE_REDACTION_VIOLATION)
      expect(result.raw_values_emitted).toBe(true)
    })

    it('GIVEN github_pat_ token in diagnostics WHEN checked THEN blocked with redaction_violation', () => {
      const result = checkEntireCLISafety({
        ...FIXTURE_SAFE,
        diagnosticStrings: ['github_pat_ABCDEFGH_LONG_TOKEN_HERE'],
      })

      expect(result.verdict).toBe('blocked')
      expect(result.reason_codes).toContain(ReasonCode.RAW_VALUE_REDACTION_VIOLATION)
    })

    it('GIVEN sk-proj- token in diagnostics WHEN checked THEN blocked with redaction_violation', () => {
      const result = checkEntireCLISafety({
        ...FIXTURE_SAFE,
        diagnosticStrings: ['sk-proj-ABCDEFGHIJKLMNOPQRSTUVWX'],
      })

      expect(result.verdict).toBe('blocked')
      expect(result.reason_codes).toContain(ReasonCode.RAW_VALUE_REDACTION_VIOLATION)
    })

    it('GIVEN AKIA prefix in diagnostics WHEN checked THEN blocked with redaction_violation', () => {
      const result = checkEntireCLISafety({
        ...FIXTURE_SAFE,
        diagnosticStrings: ['AKIAIOSFODNN7EXAMPLE'],
      })

      expect(result.verdict).toBe('blocked')
      expect(result.reason_codes).toContain(ReasonCode.RAW_VALUE_REDACTION_VIOLATION)
    })

    it('GIVEN redacted fingerprint in diagnostics WHEN checked THEN NOT blocked for redaction', () => {
      const redacted = redactFingerprint('ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ12')
      const result = checkEntireCLISafety({
        ...FIXTURE_SAFE,
        diagnosticStrings: [`token fingerprint: ${redacted}`],
      })

      // Should not be blocked for redaction violation
      expect(result.reason_codes).not.toContain(ReasonCode.RAW_VALUE_REDACTION_VIOLATION)
      expect(result.raw_values_emitted).toBe(false)
    })
  })
})
