/**
 * AC5: git config キーの検査
 * remote.*.url, remote.*.pushurl, remote.pushDefault, branch.*.pushRemote,
 * url.*.insteadOf, url.*.pushInsteadOf, include.path, includeIf.*.path,
 * remote.*.mirror, remote.*.push
 * local refs, .entire/, commit trailers, ENTIRE_CHECKPOINT_TOKEN env も対象
 * 出力は raw URL / raw config path / token を出さず reason_code と redacted fingerprint のみ
 *
 * gitConfig は multi-value: Record<string, string[]>
 */
import { describe, expect, it } from 'vitest'
import {
  checkEntireCLISafety,
  ReasonCode,
  redactFingerprint,
  containsRawValue,
} from '../../scripts/agent-logs/lib/entirecli-safety.mjs'

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

describe('entirecli-config-scan', () => {
  describe('git config parse error', () => {
    it('GIVEN git config parse error WHEN checked THEN blocked with git_config_parse_error', () => {
      const result = checkEntireCLISafety({
        ...makeSafeBase(),
        gitConfigParseErrors: ['git config -z --list failed'],
      })

      expect(result.verdict).toBe('blocked')
      expect(result.reason_codes).toContain(ReasonCode.GIT_CONFIG_PARSE_ERROR)
    })

    it('GIVEN multiple git config parse errors WHEN checked THEN only one git_config_parse_error reason', () => {
      const result = checkEntireCLISafety({
        ...makeSafeBase(),
        gitConfigParseErrors: ['error 1', 'error 2'],
      })

      const occurrences = result.reason_codes.filter((c) => c === ReasonCode.GIT_CONFIG_PARSE_ERROR)
      expect(occurrences).toHaveLength(1)
    })
  })

  describe('non-GitHub HTTP push remote detection (multi-value gitConfig)', () => {
    it('GIVEN remote.origin.pushurl with non-GitHub HTTP URL WHEN checked THEN blocked', () => {
      const result = checkEntireCLISafety({
        ...makeSafeBase(),
        gitConfig: {
          'remote.origin.pushurl': ['http://gitlab.example.com/user/repo.git'],
        },
      })

      expect(result.verdict).toBe('blocked')
      expect(result.reason_codes).toContain(ReasonCode.PUBLIC_PUSH_REMOTE_DETECTED)
    })

    it('GIVEN remote.origin.pushurl with multiple values including non-GitHub HTTP WHEN checked THEN blocked', () => {
      const result = checkEntireCLISafety({
        ...makeSafeBase(),
        gitConfig: {
          'remote.origin.pushurl': [
            'https://github.com/user/repo.git',
            'http://mirror.example.com/repo.git',
          ],
        },
      })

      expect(result.verdict).toBe('blocked')
      expect(result.reason_codes).toContain(ReasonCode.PUBLIC_PUSH_REMOTE_DETECTED)
    })

    it('GIVEN remote.origin.pushurl with GitHub URL WHEN checked THEN NOT blocked for push remote', () => {
      const result = checkEntireCLISafety({
        ...makeSafeBase(),
        gitConfig: {
          'remote.origin.pushurl': ['https://github.com/user/private-repo.git'],
        },
      })

      expect(result.reason_codes).not.toContain(ReasonCode.PUBLIC_PUSH_REMOTE_DETECTED)
    })
  })

  describe('ENTIRE_CHECKPOINT_TOKEN env presence', () => {
    it('GIVEN token env present WHEN checked THEN detected (not_applicable NOT returned)', () => {
      const result = checkEntireCLISafety({
        entireBinaryPresent: false,
        entireDirPresent: false,
        entireHooksPresent: false,
        localRefs: [],
        checkpointTrailerPresent: false,
        tokenEnvPresent: true,
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

      expect(result.verdict).not.toBe('not_applicable')
      expect(result.verdict).toBe('blocked')
    })
  })

  describe('.entire/ directory presence', () => {
    it('GIVEN entireDirPresent flag WHEN checked THEN entire detected', () => {
      const result = checkEntireCLISafety({
        entireBinaryPresent: false,
        entireDirPresent: true,
        entireHooksPresent: false,
        localRefs: [],
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

      expect(result.verdict).not.toBe('not_applicable')
    })
  })

  describe('commit trailers detection', () => {
    it('GIVEN checkpoint trailer in commits WHEN checked THEN entire detected', () => {
      const result = checkEntireCLISafety({
        ...makeSafeBase(),
        checkpointTrailerPresent: true,
        entireBinaryPresent: false,
      })

      expect(result.verdict).not.toBe('not_applicable')
    })
  })

  describe('redaction verification', () => {
    it('GIVEN redactFingerprint WHEN given a token-like string THEN does not emit raw value', () => {
      const token = 'ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234'
      const redacted = redactFingerprint(token)

      expect(redacted).not.toBe(token)
      expect(containsRawValue(redacted)).toBe(false)
    })

    it('GIVEN redactFingerprint WHEN given a URL THEN length indicator preserved', () => {
      const url = 'https://github.com/user/repo.git'
      const redacted = redactFingerprint(url)

      expect(redacted).toContain('len=')
      expect(redacted).not.toContain('github.com')
    })

    it('GIVEN raw secret in diagnosticStrings WHEN checked THEN blocked with redaction violation', () => {
      const result = checkEntireCLISafety({
        ...makeSafeBase(),
        diagnosticStrings: ['remote url: https://user:sk-ABCDEFGHIJKL@github.com/repo'],
      })

      expect(result.verdict).toBe('blocked')
      expect(result.reason_codes).toContain(ReasonCode.RAW_VALUE_REDACTION_VIOLATION)
    })

    it('GIVEN absolute path in diagnosticStrings WHEN checked THEN blocked with redaction violation', () => {
      const result = checkEntireCLISafety({
        ...makeSafeBase(),
        diagnosticStrings: ['include path: /home/user/config.gitconfig'],
      })

      expect(result.verdict).toBe('blocked')
      expect(result.reason_codes).toContain(ReasonCode.RAW_VALUE_REDACTION_VIOLATION)
    })

    it('GIVEN safe redacted diagnostics WHEN checked THEN raw_values_emitted is false', () => {
      const result = checkEntireCLISafety({
        ...makeSafeBase(),
        diagnosticStrings: [
          'checkpoint_remote: orig***[len=6]',
          'config keys scanned: 5',
        ],
      })

      expect(result.raw_values_emitted).toBe(false)
    })
  })

  describe('GIT_CONFIG_KEYS coverage', () => {
    it('GIVEN git config with mirror push WHEN checked THEN mirror key detected as push-related', () => {
      const result = checkEntireCLISafety({
        ...makeSafeBase(),
        gitConfig: {
          'remote.backup.mirror': ['true'],
          'remote.backup.url': ['http://backup.example.com/repo.git'],
        },
      })

      // mirror with non-GitHub HTTP URL → public push remote detected
      expect(result.verdict).toBe('blocked')
      expect(result.reason_codes).toContain(ReasonCode.PUBLIC_PUSH_REMOTE_DETECTED)
    })

    it('GIVEN git config with multiple remote pushurls WHEN one is public THEN blocked', () => {
      const result = checkEntireCLISafety({
        ...makeSafeBase(),
        gitConfig: {
          'remote.origin.url': ['https://github.com/user/repo.git'],
          'remote.origin.pushurl': [
            'https://github.com/user/repo.git',
            'http://public-mirror.org/repo.git',
          ],
        },
      })

      expect(result.verdict).toBe('blocked')
      expect(result.reason_codes).toContain(ReasonCode.PUBLIC_PUSH_REMOTE_DETECTED)
    })
  })

  describe('Blocker 5: settings parse error is fail-closed', () => {
    it('GIVEN base settings parse error sentinel WHEN checked THEN blocked', () => {
      const result = checkEntireCLISafety({
        ...makeSafeBase(),
        baseSettings: { parse_error: true },
      })

      expect(result.verdict).toBe('blocked')
      expect(result.reason_codes).toContain(ReasonCode.SETTINGS_PARSE_ERROR)
    })

    it('GIVEN local settings parse error sentinel WHEN checked THEN blocked', () => {
      const result = checkEntireCLISafety({
        ...makeSafeBase(),
        localSettings: { parse_error: true },
      })

      expect(result.verdict).toBe('blocked')
      expect(result.reason_codes).toContain(ReasonCode.SETTINGS_PARSE_ERROR)
    })
  })
})
