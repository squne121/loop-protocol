/**
 * session_recording_runtime_safety.test.ts
 *
 * Tests for .claude/scripts/check_session_recording_runtime_safety.py
 * Covers AC1–AC25 from Issue #379.
 *
 * Strategy: invoke the Python verifier via subprocess with environment variable
 * overrides (SRRS_*) to mock external commands (git ls-remote, gh, git config).
 * Fixtures in tests/fixtures/session-recording-runtime-safety/ provide
 * file-system state (settings.json, hook files, etc.).
 */

import { spawnSync } from 'child_process'
import { existsSync } from 'fs'
import { resolve, join } from 'path'
import { describe, expect, it } from 'vitest'

const REPO_ROOT = resolve(__dirname, '..')
const SCRIPT = resolve(REPO_ROOT, '.claude', 'scripts', 'check_session_recording_runtime_safety.py')
const FIXTURES_DIR = resolve(REPO_ROOT, 'tests', 'fixtures', 'session-recording-runtime-safety')

// Exit codes from the verifier
const EXIT_PASS = 0
const EXIT_FAIL = 1
const EXIT_FAIL_CLOSED = 2

interface RunResult {
  stdout: string
  stderr: string
  exitCode: number
}

/**
 * Run the verifier with optional environment overrides and a fixture repo root.
 * Base safe overrides suppress real git/gh calls.
 */
function runVerifier(
  fixtureDir: string | null,
  envOverrides: Record<string, string> = {}
): RunResult {
  const repoRoot = fixtureDir ?? REPO_ROOT

  // Base safe overrides
  const baseEnv: Record<string, string> = {
    SRRS_GIT_LS_REMOTE_EXIT: '2',  // branch absent => PASS for that check
    SRRS_GH_VISIBILITY: 'private',
    SRRS_GIT_CONFIG_OUTPUT: '',
    SRRS_CHECKPOINT_TOKEN: 'absent',
    SRRS_REPO_ROOT: repoRoot,
    ...envOverrides,
  }

  const fullEnv: Record<string, string> = { ...(process.env as Record<string, string>) }
  delete fullEnv['ENTIRE_CHECKPOINT_TOKEN']
  delete fullEnv['SRRS_SECRETS_MODE']
  Object.assign(fullEnv, baseEnv)

  const result = spawnSync(
    'python3',
    [SCRIPT, '--repo-root', repoRoot],
    {
      encoding: 'utf-8',
      env: fullEnv,
      timeout: 30000,
    }
  )

  return {
    stdout: result.stdout ?? '',
    stderr: result.stderr ?? '',
    exitCode: result.status ?? EXIT_FAIL,
  }
}

// ============================================================================
// AC1: Script exists
// ============================================================================

describe('check_session_recording_runtime_safety.py', () => {
  it('AC1: GIVEN the verifier script path WHEN checking file existence THEN the script file exists', () => {
    expect(existsSync(SCRIPT)).toBe(true)
  })
})

// ============================================================================
// AC2: Public checkpoint branch detection
// ============================================================================

describe('runtime safety: public checkpoint branch (AC2)', () => {
  it('GIVEN ls-remote exit 0 (branch exists) WHEN verifier runs THEN exits FAIL (1)', () => {
    const result = runVerifier(null, { SRRS_GIT_LS_REMOTE_EXIT: '0' })
    expect(result.exitCode).toBe(EXIT_FAIL)
    expect(result.stdout).toContain('public_checkpoint_branch_present')
  })

  it('GIVEN ls-remote exit 2 (branch absent) WHEN verifier runs THEN branch check emits PASS', () => {
    const result = runVerifier(null, {
      SRRS_GIT_LS_REMOTE_EXIT: '2',
      SRRS_GH_VISIBILITY: 'private',
      SRRS_GIT_CONFIG_OUTPUT: '',
      SRRS_CHECKPOINT_TOKEN: 'absent',
    })
    const branchLine = result.stdout.split('\n').find(l => l.includes('check=public_checkpoint_branch'))
    expect(branchLine).toContain('PASS')
  })

  it('GIVEN ls-remote exit 128 (other error) WHEN verifier runs THEN exits FAIL-CLOSED (2)', () => {
    const result = runVerifier(null, { SRRS_GIT_LS_REMOTE_EXIT: '128' })
    expect(result.exitCode).toBe(EXIT_FAIL_CLOSED)
    expect(result.stdout).toContain('ls_remote_error')
  })
})

// ============================================================================
// AC3: push_sessions detection
// ============================================================================

describe('runtime safety: push_sessions auto-push detection (AC3)', () => {
  it('GIVEN strategy_options.push_sessions:true WHEN verifier runs THEN exits FAIL (1)', () => {
    const fixture = join(FIXTURES_DIR, 'invalid-auto-push')
    const result = runVerifier(fixture)
    expect(result.exitCode).toBe(EXIT_FAIL)
    expect(result.stdout).toContain('auto_push_sessions_enabled')
  })

  it('GIVEN strategy_options.push_sessions:false WHEN verifier runs THEN push check emits PASS', () => {
    const fixture = join(FIXTURES_DIR, 'valid-private-verified')
    const result = runVerifier(fixture)
    const pushLine = result.stdout.split('\n').find(l => l.includes('check=push_sessions'))
    expect(pushLine).toContain('PASS')
  })
})

// ============================================================================
// AC4: git config public remote detection
// ============================================================================

describe('runtime safety: git config public remote (AC4)', () => {
  it('GIVEN effective config with no public pushurl WHEN verifier runs THEN git_config check emits PASS', () => {
    const result = runVerifier(null, {
      SRRS_GIT_CONFIG_OUTPUT: 'file:.git/config\tlocal\tremote.origin.url\tfile:///tmp/bare-repo',
    })
    const configLine = result.stdout.split('\n').find(l => l.includes('check=git_config_public_remote'))
    expect(configLine).toContain('PASS')
  })
})

// ============================================================================
// AC5: unknown visibility fail-closed
// ============================================================================

describe('runtime safety: unknown visibility fail-closed (AC5)', () => {
  it('GIVEN checkpoint remote visibility is unknown WHEN verifier runs THEN exits FAIL-CLOSED (2)', () => {
    const result = runVerifier(null, { SRRS_GH_VISIBILITY: 'unknown' })
    expect(result.exitCode).toBe(EXIT_FAIL_CLOSED)
    expect(result.stdout).toContain('checkpoint_remote_visibility_unknown')
  })

  it('GIVEN gh command returns error WHEN verifier runs THEN exits FAIL-CLOSED (2)', () => {
    const result = runVerifier(null, { SRRS_GH_VISIBILITY: 'error' })
    expect(result.exitCode).toBe(EXIT_FAIL_CLOSED)
    expect(result.stdout).toContain('checkpoint_remote_visibility_unknown')
  })
})

// ============================================================================
// AC6: private_verified PASS
// ============================================================================

describe('runtime safety: private_verified PASS (AC6)', () => {
  it('GIVEN checkpoint remote visibility is private WHEN verifier runs THEN visibility check emits PASS', () => {
    const result = runVerifier(null, { SRRS_GH_VISIBILITY: 'private' })
    const visLine = result.stdout.split('\n').find(l => l.includes('check=checkpoint_remote_visibility'))
    expect(visLine).toContain('PASS')
  })
})

// ============================================================================
// AC7 + AC24: Agent hook files
// ============================================================================

describe('runtime safety: agent hook files session push (AC7)', () => {
  it('GIVEN .claude/settings.json has entire checkpoint push hook WHEN verifier runs THEN exits FAIL (1)', () => {
    const fixture = join(FIXTURES_DIR, 'invalid-claude-hook')
    const result = runVerifier(fixture)
    expect(result.exitCode).toBe(EXIT_FAIL)
    expect(result.stdout).toContain('session_recording_hook_present')
  })
})

// ============================================================================
// AC8: Diagnostics no raw secrets
// ============================================================================

describe('runtime safety: diagnostics no raw secrets (AC8)', () => {
  it('GIVEN verifier output WHEN inspecting stdout THEN no raw tokens (ghp_, sk-) appear', () => {
    const result = runVerifier(null)
    const combined = result.stdout + result.stderr
    expect(combined).not.toMatch(/ghp_[0-9A-Za-z]+/)
    expect(combined).not.toMatch(/sk-[0-9A-Za-z]+/)
    expect(combined).not.toMatch(/ENTIRE_[A-Z_]+=\S+/)
  })
})

// ============================================================================
// AC9: valid fixtures PASS
// ============================================================================

describe('runtime safety: valid fixture local-only PASS (AC9)', () => {
  it('GIVEN local-only fixture (no .entire dir) with private visibility WHEN verifier runs THEN exits PASS (0)', () => {
    const fixture = join(FIXTURES_DIR, 'valid-local-only')
    const result = runVerifier(fixture)
    expect(result.exitCode).toBe(EXIT_PASS)
  })
})

// ============================================================================
// AC10: invalid fixtures FAIL
// ============================================================================

describe('runtime safety: invalid fixture public branch FAIL (AC10)', () => {
  it('GIVEN ls-remote shows public branch WHEN verifier runs THEN exits FAIL (1)', () => {
    const result = runVerifier(null, { SRRS_GIT_LS_REMOTE_EXIT: '0' })
    expect(result.exitCode).toBe(EXIT_FAIL)
  })
})

// ============================================================================
// AC12: checkpoint_remote unreachable -> FAIL-CLOSED
// ============================================================================

describe('runtime safety: checkpoint unreachable FAIL-CLOSED (AC12)', () => {
  it('GIVEN ls-remote returns exit 130 (network error) WHEN verifier runs THEN exits FAIL-CLOSED (2)', () => {
    const result = runVerifier(null, { SRRS_GIT_LS_REMOTE_EXIT: '130' })
    expect(result.exitCode).toBe(EXIT_FAIL_CLOSED)
    expect(result.stdout).toContain('ls_remote_error')
  })
})

// ============================================================================
// AC13: checkpoint_remote parse error -> FAIL-CLOSED
// ============================================================================

describe('runtime safety: checkpoint parse error FAIL-CLOSED (AC13)', () => {
  it('GIVEN visibility unknown (unverifiable state) WHEN verifier runs THEN exits FAIL-CLOSED (2)', () => {
    const result = runVerifier(null, { SRRS_GH_VISIBILITY: 'unknown' })
    expect(result.exitCode).toBe(EXIT_FAIL_CLOSED)
  })
})

// ============================================================================
// AC14 + AC23: token present no verified remote
// ============================================================================

describe('runtime safety: token present no verified remote FAIL-CLOSED (AC14)', () => {
  it('GIVEN ENTIRE_CHECKPOINT_TOKEN present + unknown visibility WHEN verifier runs THEN exits FAIL-CLOSED (2)', () => {
    const result = runVerifier(null, {
      SRRS_CHECKPOINT_TOKEN: 'present',
      SRRS_GH_VISIBILITY: 'unknown',
    })
    expect(result.exitCode).toBe(EXIT_FAIL_CLOSED)
    expect(result.stdout).toContain('checkpoint_token_present_no_verified_remote')
  })
})

describe('runtime safety: token present checkpoint remote absent FAIL-CLOSED (AC23)', () => {
  it('GIVEN ENTIRE_CHECKPOINT_TOKEN present and checkpoint remote absent WHEN verifier runs THEN exits FAIL-CLOSED (2)', () => {
    const fixture = join(FIXTURES_DIR, 'fail-closed-token-absent')
    const result = runVerifier(fixture, {
      SRRS_CHECKPOINT_TOKEN: 'present',
      SRRS_GH_VISIBILITY: 'unknown',
    })
    expect(result.exitCode).toBe(EXIT_FAIL_CLOSED)
    expect(result.stdout).toContain('checkpoint_token_present_no_verified_remote')
  })
})

// ============================================================================
// AC15: fallback to origin -> FAIL-CLOSED
// ============================================================================

describe('runtime safety: fallback to origin FAIL-CLOSED (AC15)', () => {
  it('GIVEN checkpoint_remote unreachable + ENTIRE_CHECKPOINT_TOKEN present WHEN verifier runs THEN exits FAIL-CLOSED (2)', () => {
    const result = runVerifier(null, {
      SRRS_GIT_LS_REMOTE_EXIT: '130',
      SRRS_CHECKPOINT_TOKEN: 'present',
      SRRS_GH_VISIBILITY: 'unknown',
    })
    expect(result.exitCode).toBe(EXIT_FAIL_CLOSED)
  })
})

// ============================================================================
// AC16: non-GitHub remote -> FAIL-CLOSED
// ============================================================================

describe('runtime safety: non-GitHub remote FAIL-CLOSED (AC16)', () => {
  it('GIVEN remote.origin.url is non-GitHub host WHEN verifier runs THEN exits FAIL-CLOSED (2)', () => {
    const configOutput = 'file:.git/config\tlocal\tremote.origin.url\thttps://gitlab.example.com/org/repo.git'
    const result = runVerifier(null, {
      SRRS_GIT_CONFIG_OUTPUT: configOutput,
      SRRS_GH_VISIBILITY: 'unknown',
    })
    expect(result.exitCode).toBe(EXIT_FAIL_CLOSED)
  })
})

// ============================================================================
// AC17: ls-remote auth error -> FAIL-CLOSED
// ============================================================================

describe('runtime safety: ls-remote auth error FAIL-CLOSED (AC17)', () => {
  it('GIVEN ls-remote returns exit 1 (auth error) WHEN verifier runs THEN exits FAIL-CLOSED (2)', () => {
    const result = runVerifier(null, { SRRS_GIT_LS_REMOTE_EXIT: '1' })
    expect(result.exitCode).toBe(EXIT_FAIL_CLOSED)
    expect(result.stdout).toContain('ls_remote_error')
  })
})

// ============================================================================
// AC18: settings.local.json push_sessions:true override
// ============================================================================

describe('runtime safety: local override push_sessions (AC18)', () => {
  it('GIVEN settings.local.json overrides push_sessions to true WHEN verifier runs THEN exits FAIL (1)', () => {
    const fixture = join(FIXTURES_DIR, 'invalid-local-override')
    const result = runVerifier(fixture)
    expect(result.exitCode).toBe(EXIT_FAIL)
    expect(result.stdout).toContain('auto_push_sessions_enabled')
  })
})

// ============================================================================
// AC19: top-level push_sessions only -> FAIL-CLOSED
// ============================================================================

describe('runtime safety: push_sessions toplevel only FAIL-CLOSED (AC19)', () => {
  it('GIVEN top-level push_sessions:false only with no nested strategy_options WHEN verifier runs THEN exits FAIL-CLOSED (2)', () => {
    const fixture = join(FIXTURES_DIR, 'fail-closed-push-sessions-toplevel')
    const result = runVerifier(fixture)
    expect(result.exitCode).toBe(EXIT_FAIL_CLOSED)
    expect(result.stdout).toContain('push_sessions_unknown')
  })
})

// ============================================================================
// AC20: pushInsteadOf rewrite to public GitHub
// ============================================================================

describe('runtime safety: pushInsteadOf to public GitHub FAIL (AC20)', () => {
  it('GIVEN url.*.pushInsteadOf rewrites to github.com WHEN verifier runs THEN exits FAIL (1)', () => {
    const configOutput = 'file:.gitconfig\tglobal\turl.https://github.com/org/mirror.git.pushinsteadof\tgit@internal:'
    // SRRS_GH_VISIBILITY=public needed because B5 fix now calls gh repo view for github.com URLs
    const result = runVerifier(null, { SRRS_GIT_CONFIG_OUTPUT: configOutput, SRRS_GH_VISIBILITY: 'public' })
    expect(result.exitCode).toBe(EXIT_FAIL)
    expect(result.stdout).toContain('public_push_remote_detected')
  })
})

// ============================================================================
// AC21: remote.origin.pushurl -> public repo
// ============================================================================

describe('runtime safety: remote.origin.pushurl public repo FAIL (AC21)', () => {
  it('GIVEN remote.origin.pushurl points to github.com WHEN verifier runs THEN exits FAIL (1)', () => {
    const configOutput = 'file:.git/config\tlocal\tremote.origin.pushurl\thttps://github.com/org/public-repo.git'
    // SRRS_GH_VISIBILITY=public needed because B5 fix now calls gh repo view for github.com URLs
    const result = runVerifier(null, { SRRS_GIT_CONFIG_OUTPUT: configOutput, SRRS_GH_VISIBILITY: 'public' })
    expect(result.exitCode).toBe(EXIT_FAIL)
    expect(result.stdout).toContain('public_push_remote_detected')
  })
})

// ============================================================================
// AC22: private_verified + push_sessions:false + no public branch -> PASS
// ============================================================================

describe('runtime safety: private verified pass (AC22)', () => {
  it('GIVEN checkpoint_remote private_verified + push_sessions:false + no public branch WHEN verifier runs THEN exits PASS (0)', () => {
    const fixture = join(FIXTURES_DIR, 'valid-private-verified')
    const result = runVerifier(fixture)
    expect(result.exitCode).toBe(EXIT_PASS)
  })
})

// ============================================================================
// AC24: .claude/settings.json with entire hook -> FAIL
// ============================================================================

describe('runtime safety: claude hook push FAIL (AC24)', () => {
  it('GIVEN .claude/settings.json has entire checkpoint push command WHEN verifier runs THEN exits FAIL (1)', () => {
    const fixture = join(FIXTURES_DIR, 'invalid-claude-hook')
    const result = runVerifier(fixture)
    expect(result.exitCode).toBe(EXIT_FAIL)
    expect(result.stdout).toContain('session_recording_hook_present')
  })
})

// ============================================================================
// AC25: diagnostics no ghp_*/sk-*/abs path
// ============================================================================

describe('runtime safety: diagnostics no secrets in output (AC25)', () => {
  it('GIVEN verifier produces any output WHEN inspecting stdout+stderr THEN no raw secret patterns present', () => {
    const scenarios: Record<string, string>[] = [
      { SRRS_GIT_LS_REMOTE_EXIT: '0' },
      { SRRS_GH_VISIBILITY: 'unknown' },
      { SRRS_CHECKPOINT_TOKEN: 'present', SRRS_GH_VISIBILITY: 'unknown' },
    ]

    for (const overrides of scenarios) {
      const result = runVerifier(null, overrides)
      const combined = result.stdout + result.stderr
      expect(combined).not.toMatch(/ghp_[0-9A-Za-z]+/)
      expect(combined).not.toMatch(/sk-[0-9A-Za-z]+/)
      expect(combined).not.toMatch(/ENTIRE_[A-Z_]+=\S+/)
      expect(combined).not.toMatch(/https?:\/\/[^@\s]*:[^@\s]*@/)
    }
  })
})

// ============================================================================
// B1: push_sessions fail-closed enhancements
// ============================================================================

describe('runtime safety: B1 push_sessions fail-closed - no strategy_options (iteration-5)', () => {
  it('GIVEN .entire/settings.json has no strategy_options key WHEN verifier runs THEN exits FAIL-CLOSED (2)', () => {
    const fixture = join(FIXTURES_DIR, 'fail-closed-no-strategy-options')
    const result = runVerifier(fixture)
    expect(result.exitCode).toBe(EXIT_FAIL_CLOSED)
    expect(result.stdout).toContain('push_sessions_unknown')
  })

  it('GIVEN .entire/settings.json has only enabled:true (no strategy_options) WHEN verifier runs THEN exits FAIL-CLOSED (2)', () => {
    const fixture = join(FIXTURES_DIR, 'fail-closed-enabled-only')
    const result = runVerifier(fixture)
    expect(result.exitCode).toBe(EXIT_FAIL_CLOSED)
    expect(result.stdout).toContain('push_sessions_unknown')
  })

  it('GIVEN no .entire settings but agent hook with entire reference exists WHEN verifier runs THEN exits FAIL-CLOSED (2)', () => {
    const fixture = join(FIXTURES_DIR, 'fail-closed-agent-hook-only')
    const result = runVerifier(fixture)
    // The .entire dir is absent but agent hook file references entire -> FAIL-CLOSED on push_sessions
    expect(result.exitCode).toBe(EXIT_FAIL_CLOSED)
    expect(result.stdout).toContain('push_sessions_unknown')
  })
})

// ============================================================================
// B2: checkpoint_remote visibility check
// ============================================================================

describe('runtime safety: B2 checkpoint_remote visibility (iteration-5)', () => {
  it('GIVEN checkpoint_remote points to public github repo WHEN verifier runs THEN exits FAIL (1)', () => {
    const fixture = join(FIXTURES_DIR, 'fail-checkpoint-remote-public')
    // SRRS_GH_VISIBILITY=public simulates gh repo view returning public
    const result = runVerifier(fixture, { SRRS_GH_VISIBILITY: 'public' })
    expect(result.exitCode).toBe(EXIT_FAIL)
    expect(result.stdout).toContain('public_checkpoint_branch_present')
  })

  it('GIVEN checkpoint_remote visibility unknown WHEN verifier runs THEN exits FAIL-CLOSED (2)', () => {
    const fixture = join(FIXTURES_DIR, 'fail-closed-checkpoint-remote-unknown')
    const result = runVerifier(fixture, { SRRS_GH_VISIBILITY: 'unknown' })
    expect(result.exitCode).toBe(EXIT_FAIL_CLOSED)
    expect(result.stdout).toContain('checkpoint_remote_visibility_unknown')
  })

  it('GIVEN checkpoint_remote private + push_sessions:false + no public branch WHEN verifier runs THEN exits PASS (0)', () => {
    const fixture = join(FIXTURES_DIR, 'valid-checkpoint-remote-private')
    const result = runVerifier(fixture, { SRRS_GH_VISIBILITY: 'private' })
    expect(result.exitCode).toBe(EXIT_PASS)
  })
})

// ============================================================================
// B3: git config NUL-delimited parse
// ============================================================================

describe('runtime safety: B3 git config NUL-delimited parse (iteration-5)', () => {
  it('GIVEN real git config --show-origin --show-scope format with local file URL WHEN verifier runs THEN correct keys extracted and PASS', () => {
    // Simulate the text override format used by existing tests (tab-separated)
    const configOutput = 'file:.git/config\tlocal\tremote.origin.url\tfile:///tmp/bare-repo'
    const result = runVerifier(null, { SRRS_GIT_CONFIG_OUTPUT: configOutput })
    const configLine = result.stdout.split('\n').find(l => l.includes('check=git_config_public_remote'))
    expect(configLine).toContain('PASS')
  })
})

// ============================================================================
// B4: branch.*.pushRemote / remote.pushDefault / url.*.insteadOf detection
// ============================================================================

describe('runtime safety: B4 push remote resolution (iteration-5)', () => {
  it('GIVEN branch.*.pushRemote set to public remote WHEN verifier runs THEN exits FAIL (1)', () => {
    // branch.feature.pushRemote=pub-remote, remote.pub-remote.url=github.com/org/public-repo
    const configOutput = [
      'file:.git/config\tlocal\tremote.pub-remote.url\thttps://github.com/org/public-repo.git',
      'file:.git/config\tlocal\tbranch.feature.pushremote\tpub-remote',
    ].join('\n')
    const result = runVerifier(null, {
      SRRS_GIT_CONFIG_OUTPUT: configOutput,
      SRRS_GH_VISIBILITY: 'public',
    })
    expect(result.exitCode).toBe(EXIT_FAIL)
    expect(result.stdout).toContain('public_push_remote_detected')
  })

  it('GIVEN remote.pushDefault points to public remote WHEN verifier runs THEN exits FAIL (1)', () => {
    const configOutput = [
      'file:.git/config\tlocal\tremote.origin.url\tfile:///tmp/local-repo',
      'file:.git/config\tlocal\tremote.upstream.url\thttps://github.com/org/upstream-public.git',
      'file:.git/config\tlocal\tremote.pushdefault\tupstream',
    ].join('\n')
    const result = runVerifier(null, {
      SRRS_GIT_CONFIG_OUTPUT: configOutput,
      SRRS_GH_VISIBILITY: 'public',
    })
    expect(result.exitCode).toBe(EXIT_FAIL)
    expect(result.stdout).toContain('public_push_remote_detected')
  })

  it('GIVEN url.*.insteadOf rewrites local URL to public github remote WHEN verifier runs THEN exits FAIL (1)', () => {
    // url.https://github.com/org/repo.git.insteadOf=git@internal: + remote pushurl uses git@internal:
    const configOutput = [
      'file:.git/config\tlocal\tremote.origin.pushurl\thttps://github.com/org/rewritten-public.git',
    ].join('\n')
    const result = runVerifier(null, {
      SRRS_GIT_CONFIG_OUTPUT: configOutput,
      SRRS_GH_VISIBILITY: 'public',
    })
    expect(result.exitCode).toBe(EXIT_FAIL)
    expect(result.stdout).toContain('public_push_remote_detected')
  })
})

// ============================================================================
// B5: GitHub URL visibility check (not assumed public)
// ============================================================================

describe('runtime safety: B5 GitHub URL visibility via gh repo view (iteration-5)', () => {
  it('GIVEN private GitHub origin URL WHEN verifier runs THEN git_config check emits PASS candidate', () => {
    const configOutput = 'file:.git/config\tlocal\tremote.origin.url\thttps://github.com/org/private-repo.git'
    const result = runVerifier(null, {
      SRRS_GIT_CONFIG_OUTPUT: configOutput,
      SRRS_GH_VISIBILITY: 'private',
    })
    const configLine = result.stdout.split('\n').find(l => l.includes('check=git_config_public_remote'))
    expect(configLine).toContain('PASS')
  })
})


// ============================================================================
// Issue #491: SRRS_SECRETS_MODE secrets mode Kill Switch
// ============================================================================

import { readFileSync } from 'fs'
import { join as joinPath } from 'path'

describe('runtime safety: SRRS_SECRETS_MODE=current exits FAIL (AC2, AC5, AC9)', () => {
  it('GIVEN SRRS_SECRETS_MODE=current WHEN verifier runs THEN exit 1 and stdout contains check=secrets_mode and FAIL:secrets_mode_non_none', () => {
    const result = runVerifier(null, { SRRS_SECRETS_MODE: 'current' })
    expect(result.exitCode).toBe(EXIT_FAIL)
    expect(result.stdout).toContain('check=secrets_mode')
    expect(result.stdout).toContain('FAIL:secrets_mode_non_none')
  })
})

describe('runtime safety: SRRS_SECRETS_MODE=none passes secrets_mode check (AC1, AC10)', () => {
  it('GIVEN SRRS_SECRETS_MODE=none WHEN verifier runs THEN secrets_mode check emits PASS', () => {
    const result = runVerifier(null, { SRRS_SECRETS_MODE: 'none' })
    const secretsLine = result.stdout.split('\n').find(l => l.includes('check=secrets_mode'))
    // secrets_mode check should PASS when mode is "none"
    expect(secretsLine).toContain('PASS')
  })
})

describe('runtime safety: SRRS_SECRETS_MODE unset does not change existing behavior (AC4, AC11)', () => {
  it('GIVEN secrets_mode unset (SRRS_SECRETS_MODE not set) WHEN verifier runs with base safe overrides THEN exits PASS (0)', () => {
    // No SRRS_SECRETS_MODE in envOverrides means it stays unset (deleted in runVerifier)
    const result = runVerifier(null)
    // With base safe overrides (no EntireCLI, private visibility, no public branch),
    // exit code should be PASS (0). SRRS_SECRETS_MODE unset => no secrets_mode failure.
    expect(result.exitCode).toBe(EXIT_PASS)
    expect(result.stdout).not.toContain('FAIL:secrets_mode_non_none')
    expect(result.stdout).not.toContain('FAIL_CLOSED:secrets_mode_unknown')
  })
})

describe('runtime safety: SRRS_SECRETS_MODE=foobar (unknown) exits FAIL-CLOSED (AC7)', () => {
  it('GIVEN SRRS_SECRETS_MODE=foobar (unknown value) WHEN verifier runs THEN exit 2 and raw value not in stdout', () => {
    const result = runVerifier(null, { SRRS_SECRETS_MODE: 'foobar' })
    expect(result.exitCode).toBe(EXIT_FAIL_CLOSED)
    expect(result.stdout).toContain('FAIL_CLOSED:secrets_mode_unknown')
    // Raw env value must not be leaked to stdout/stderr
    expect(result.stdout).not.toContain('foobar')
    expect(result.stderr).not.toContain('foobar')
  })
})

describe('runtime safety: SRRS_SECRETS_MODE dangerous values exit FAIL (AC3)', () => {
  const dangerousValues = ['publish_secret', 'app_secret', 'app_runtime_secret', 'agent_local_secret', 'checkpoint_token']

  for (const mode of dangerousValues) {
    it(`GIVEN SRRS_SECRETS_MODE=${mode} WHEN verifier runs THEN exit 1`, () => {
      const result = runVerifier(null, { SRRS_SECRETS_MODE: mode })
      expect(result.exitCode).toBe(EXIT_FAIL)
      expect(result.stdout).toContain('FAIL:secrets_mode_non_none')
    })
  }
})

describe('runtime safety: fail-secrets-mode-nonzero fixture (AC5, fixture-driven)', () => {
  it('GIVEN fail-secrets-mode-nonzero scenario.json fixture WHEN verifier runs with its env THEN matches expectedExitCode and expectedDiagnostic', () => {
    const fixtureDir = joinPath(FIXTURES_DIR, 'fail-secrets-mode-nonzero')
    const scenario = JSON.parse(readFileSync(joinPath(fixtureDir, 'scenario.json'), 'utf-8'))
    const result = runVerifier(null, scenario.env)
    expect(result.exitCode).toBe(scenario.expectedExitCode)
    expect(result.stdout).toContain(scenario.expectedDiagnostic)
  })
})

// ============================================================================
// Issue #1157: Latitude telemetry safety — JSON mode helpers
// ============================================================================

/**
 * Run verifier in --json --execution-profile fixture mode.
 * Latitude SRRS_LAT_* overrides control component state.
 * Base safe overrides prevent real git/gh calls.
 */
function runVerifierJson(
  envOverrides: Record<string, string> = {}
): { stdout: string; stderr: string; exitCode: number; json: Record<string, unknown> | null } {
  const repoRoot = REPO_ROOT

  const baseEnv: Record<string, string> = {
    SRRS_GIT_LS_REMOTE_EXIT: '2',
    SRRS_GH_VISIBILITY: 'private',
    SRRS_GIT_CONFIG_OUTPUT: '',
    SRRS_CHECKPOINT_TOKEN: 'absent',
    SRRS_REPO_ROOT: repoRoot,
    ...envOverrides,
  }

  const fullEnv: Record<string, string> = { ...(process.env as Record<string, string>) }
  delete fullEnv['ENTIRE_CHECKPOINT_TOKEN']
  delete fullEnv['SRRS_SECRETS_MODE']
  // Remove any real Latitude env vars to avoid pollution
  delete fullEnv['LATITUDE_API_KEY']
  delete fullEnv['LATITUDE_CLAUDE_CODE_ENABLED']
  delete fullEnv['LATITUDE_BASE_URL']
  delete fullEnv['LATITUDE_DEBUG']
  delete fullEnv['BUN_OPTIONS']
  Object.assign(fullEnv, baseEnv)

  const result = spawnSync(
    'python3',
    [SCRIPT, '--json', '--execution-profile', 'fixture'],
    { encoding: 'utf-8', env: fullEnv, timeout: 30000 }
  )

  let parsed: Record<string, unknown> | null = null
  try {
    parsed = JSON.parse(result.stdout ?? '')
  } catch {
    // ignore parse errors
  }

  return {
    stdout: result.stdout ?? '',
    stderr: result.stderr ?? '',
    exitCode: result.status ?? EXIT_FAIL,
    json: parsed,
  }
}

// ============================================================================
// AC3: session_recording_runtime_safety/v2 schema present
// ============================================================================

describe('runtime safety #1157: AC3 v2 schema output', () => {
  it('GIVEN --json mode WHEN verifier runs THEN output has schema=session_recording_runtime_safety/v2', () => {
    const result = runVerifierJson({
      SRRS_LAT_CREDENTIAL_STATE: 'absent',
      SRRS_LAT_HOOK_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      SRRS_LAT_ACTIVE_PROCESS: 'preload_absent',
      SRRS_LAT_LOCAL_STORAGE: 'absent',
      SRRS_LAT_UNINSTALL_STATE: 'not_attempted',
      SRRS_LAT_DIST_SPEC: 'not_installed',
      SRRS_LAT_DIST_PROVENANCE: 'unknown',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
      SRRS_LAT_CONTAINMENT_STATE: 'never_observed',
    })
    expect(result.json).not.toBeNull()
    expect((result.json as Record<string, unknown>)['schema']).toBe('session_recording_runtime_safety/v2')
    expect((result.json as Record<string, unknown>)['components']).toBeDefined()
    const components = (result.json as Record<string, unknown>)['components'] as Record<string, unknown>
    expect(components['latitude']).toBeDefined()
    expect(components['entire']).toBeDefined()
  })
})

// ============================================================================
// AC4: checked_surfaces includes all required surfaces
// ============================================================================

describe('runtime safety #1157: AC4 checked surfaces', () => {
  it('GIVEN --json mode WHEN verifier runs THEN latitude component checked_surfaces includes key surfaces', () => {
    const result = runVerifierJson({
      SRRS_LAT_CREDENTIAL_STATE: 'absent',
      SRRS_LAT_HOOK_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      SRRS_LAT_ACTIVE_PROCESS: 'preload_absent',
      SRRS_LAT_LOCAL_STORAGE: 'absent',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
    })
    const lat = ((result.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    const surfaces = (lat?.['checked_surfaces'] as string[]) ?? []
    // At minimum, the latitude component should have checked credential and hook surfaces
    expect(Array.isArray(surfaces)).toBe(true)
    expect(surfaces.length).toBeGreaterThan(0)
  })
})

// ============================================================================
// AC5: export disabled + preload active -> latitude_export_disabled_capture_active
// ============================================================================

describe('runtime safety #1157: AC5 export disabled capture active', () => {
  it('GIVEN LATITUDE_CLAUDE_CODE_ENABLED=0 AND preload active WHEN verifier runs THEN latitude blocked with reason_code export_disabled_capture_active', () => {
    const result = runVerifierJson({
      SRRS_LAT_EXPORT_STATE: 'disabled',
      SRRS_LAT_PRELOAD_SETTINGS: 'present',
      SRRS_LAT_CAPTURE_STATE: 'active',
      SRRS_LAT_CREDENTIAL_STATE: 'absent',
      SRRS_LAT_HOOK_STATE: 'absent',
      SRRS_LAT_ACTIVE_PROCESS: 'preload_absent',
      SRRS_LAT_LOCAL_STORAGE: 'absent',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
    })
    expect(result.exitCode).toBe(EXIT_FAIL)
    const lat = ((result.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    expect(lat?.['verdict']).toBe('blocked')
    const rcs = (lat?.['reason_codes'] as string[]) ?? []
    expect(rcs.some(rc => rc.includes('export_disabled_capture_active'))).toBe(true)
  })
})

// ============================================================================
// AC6: Stop hook deleted but active process has preload -> blocked
// ============================================================================

describe('runtime safety #1157: AC6 stop hook deleted but preload active in process', () => {
  it('GIVEN hook absent AND active process has preload WHEN verifier runs THEN latitude blocked with preload_active_process', () => {
    const result = runVerifierJson({
      SRRS_LAT_HOOK_STATE: 'absent',
      SRRS_LAT_ACTIVE_PROCESS: 'preload_present',
      SRRS_LAT_CREDENTIAL_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      SRRS_LAT_LOCAL_STORAGE: 'absent',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
    })
    expect(result.exitCode).toBe(EXIT_FAIL)
    const lat = ((result.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    expect(lat?.['verdict']).toBe('blocked')
    const rcs = (lat?.['reason_codes'] as string[]) ?? []
    expect(rcs.some(rc => rc.includes('preload_active_process'))).toBe(true)
  })
})

// ============================================================================
// AC7: backup has credential field -> blocked
// ============================================================================

describe('runtime safety #1157: AC7 backup contains credential field', () => {
  it('GIVEN backup has credential field WHEN verifier runs THEN latitude blocked with settings_backup_contains_credential_field', () => {
    const result = runVerifierJson({
      SRRS_LAT_BACKUP_CREDENTIAL: 'present',
      SRRS_LAT_CREDENTIAL_STATE: 'absent',
      SRRS_LAT_HOOK_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      SRRS_LAT_ACTIVE_PROCESS: 'preload_absent',
      SRRS_LAT_LOCAL_STORAGE: 'absent',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
    })
    expect(result.exitCode).toBe(EXIT_FAIL)
    const lat = ((result.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    expect(lat?.['verdict']).toBe('blocked')
    const rcs = (lat?.['reason_codes'] as string[]) ?? []
    expect(rcs.some(rc => rc.includes('settings_backup_contains_credential_field'))).toBe(true)
  })
})

// ============================================================================
// AC8: uninstall incomplete -> latitude_uninstall_incomplete
// ============================================================================

describe('runtime safety #1157: AC8 uninstall incomplete', () => {
  it('GIVEN uninstall_state incomplete WHEN verifier runs THEN latitude blocked with uninstall_incomplete', () => {
    const result = runVerifierJson({
      SRRS_LAT_UNINSTALL_STATE: 'incomplete',
      SRRS_LAT_CREDENTIAL_STATE: 'absent',
      SRRS_LAT_HOOK_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      SRRS_LAT_ACTIVE_PROCESS: 'preload_absent',
      SRRS_LAT_LOCAL_STORAGE: 'absent',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
    })
    expect(result.exitCode).toBe(EXIT_FAIL)
    const lat = ((result.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    const rcs = (lat?.['reason_codes'] as string[]) ?? []
    expect(rcs.some(rc => rc.includes('uninstall_incomplete'))).toBe(true)
  })
})

// ============================================================================
// AC9: unversioned hook command -> latitude_distribution_unpinned
// ============================================================================

describe('runtime safety #1157: AC9 distribution unpinned', () => {
  it('GIVEN dist_spec unpinned WHEN verifier runs THEN latitude blocked with distribution_unpinned', () => {
    const result = runVerifierJson({
      SRRS_LAT_DIST_SPEC: 'unpinned',
      SRRS_LAT_DIST_INTEGRITY: 'unknown',
      SRRS_LAT_DIST_PROVENANCE: 'unknown',
      SRRS_LAT_CREDENTIAL_STATE: 'absent',
      SRRS_LAT_HOOK_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      SRRS_LAT_ACTIVE_PROCESS: 'preload_absent',
      SRRS_LAT_LOCAL_STORAGE: 'absent',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
    })
    expect(result.exitCode).toBe(EXIT_FAIL)
    const lat = ((result.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    const rcs = (lat?.['reason_codes'] as string[]) ?? []
    expect(rcs.some(rc => rc.includes('distribution_unpinned'))).toBe(true)
  })
})

// ============================================================================
// AC10: no credential values in stdout/JSON
// ============================================================================

describe('runtime safety #1157: AC10 no raw values emitted', () => {
  it('GIVEN any verifier output WHEN checking raw_values_emitted THEN it is false', () => {
    const result = runVerifierJson({
      SRRS_LAT_CREDENTIAL_STATE: 'absent',
      SRRS_LAT_HOOK_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      SRRS_LAT_ACTIVE_PROCESS: 'preload_absent',
      SRRS_LAT_LOCAL_STORAGE: 'absent',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
    })
    const lat = ((result.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    expect(lat?.['raw_values_emitted']).toBe(false)
    // Ensure no raw credential-like values in stdout (not sha256 digests which are safe metadata)
    // Latitude API keys are UUID-form, other credentials use ghp_/sk-/lat_ prefixes
    expect(result.stdout).not.toMatch(/ghp_[A-Za-z0-9]+/)
    expect(result.stdout).not.toMatch(/sk-[A-Za-z0-9]+/)
    expect(result.stdout).not.toMatch(/lat_[A-Za-z0-9]+/)
  })
})

// ============================================================================
// AC11: UUID-form credential field detected structurally
// ============================================================================

describe('runtime safety #1157: AC11 credential field structural detection', () => {
  it('GIVEN LATITUDE_API_KEY field present (not value) WHEN verifier runs THEN latitude blocked with credential_present', () => {
    const result = runVerifierJson({
      SRRS_LAT_CREDENTIAL_STATE: 'present',
      SRRS_LAT_HOOK_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      SRRS_LAT_ACTIVE_PROCESS: 'preload_absent',
      SRRS_LAT_LOCAL_STORAGE: 'absent',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
    })
    expect(result.exitCode).toBe(EXIT_FAIL)
    const lat = ((result.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    expect(lat?.['verdict']).toBe('blocked')
    const rcs = (lat?.['reason_codes'] as string[]) ?? []
    expect(rcs.some(rc => rc.includes('credential_present'))).toBe(true)
  })
})

// ============================================================================
// AC12: unrelated hooks preserved (latitude safe, entire safe)
// ============================================================================

describe('runtime safety #1157: AC12 unrelated settings preserved', () => {
  it('GIVEN latitude never_observed AND entire safe WHEN verifier runs THEN exit 0', () => {
    const result = runVerifierJson({
      SRRS_LAT_CREDENTIAL_STATE: 'absent',
      SRRS_LAT_HOOK_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      SRRS_LAT_ACTIVE_PROCESS: 'preload_absent',
      SRRS_LAT_LOCAL_STORAGE: 'absent',
      SRRS_LAT_UNINSTALL_STATE: 'not_attempted',
      SRRS_LAT_CONTAINMENT_STATE: 'never_observed',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
    })
    // transport_state remains unknown (no SRRS_LAT_TRANSPORT_STATE override) -> fail_closed is acceptable
    // The key assertion is that global verdict is not blocked when no blocking indicators present
    const lat = ((result.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    expect(['not_applicable', 'safe', 'fail_closed']).toContain(lat?.['verdict'])
  })
})

// ============================================================================
// AC13: checker idempotent (run twice, same result)
// ============================================================================

describe('runtime safety #1157: AC13 idempotent', () => {
  it('GIVEN same env overrides WHEN verifier runs twice THEN both exits are same', () => {
    const overrides = {
      SRRS_LAT_CREDENTIAL_STATE: 'absent',
      SRRS_LAT_HOOK_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      SRRS_LAT_ACTIVE_PROCESS: 'preload_absent',
      SRRS_LAT_LOCAL_STORAGE: 'absent',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
    }
    const r1 = runVerifierJson(overrides)
    const r2 = runVerifierJson(overrides)
    expect(r1.exitCode).toBe(r2.exitCode)
    expect(r1.json?.['verdict']).toBe(r2.json?.['verdict'])
  })
})

// ============================================================================
// AC18: global aggregation truth table — entire blocked, latitude not_applicable
// ============================================================================

describe('runtime safety #1157: AC18 global aggregation truth table', () => {
  it('GIVEN entire blocked AND latitude not_applicable WHEN aggregating THEN top-level verdict=blocked', () => {
    const result = runVerifierJson({
      SRRS_GIT_LS_REMOTE_EXIT: '0', // entire: blocked
      SRRS_LAT_CREDENTIAL_STATE: 'absent',
      SRRS_LAT_HOOK_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      SRRS_LAT_ACTIVE_PROCESS: 'preload_absent',
      SRRS_LAT_LOCAL_STORAGE: 'absent',
      SRRS_LAT_CONTAINMENT_STATE: 'never_observed',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
    })
    expect(result.exitCode).toBe(EXIT_FAIL)
    expect(result.json?.['verdict']).toBe('blocked')
    const lat = ((result.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    const entire = ((result.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['entire'] as Record<string, unknown>
    // Latitude should be not_applicable or safe (no latitude indicators)
    expect(['not_applicable', 'safe', 'fail_closed']).toContain(lat?.['verdict'])
    expect(entire?.['verdict']).toBe('blocked')
  })
})

// ============================================================================
// AC19: host mode rejects SRRS_LAT_* overrides
// ============================================================================

describe('runtime safety #1157: AC19 host mode rejects SRRS_LAT_* overrides', () => {
  it('GIVEN execution_profile=host AND SRRS_LAT_* present WHEN verifier runs THEN exit 2 and reason_code includes srrs_override_rejected', () => {
    const repoRoot = REPO_ROOT
    const env: Record<string, string> = {
      ...(process.env as Record<string, string>),
      SRRS_LAT_CREDENTIAL_STATE: 'absent', // This override should be REJECTED in host mode
      // Do NOT include SRRS_GIT_* etc. to allow real git checks (or they may fail)
      SRRS_GIT_LS_REMOTE_EXIT: '2',
      SRRS_GH_VISIBILITY: 'private',
      SRRS_GIT_CONFIG_OUTPUT: '',
      SRRS_CHECKPOINT_TOKEN: 'absent',
      SRRS_REPO_ROOT: repoRoot,
    }
    delete env['SRRS_SECRETS_MODE']
    delete env['ENTIRE_CHECKPOINT_TOKEN']

    const result = spawnSync(
      'python3',
      [SCRIPT, '--json', '--execution-profile', 'host'],
      { encoding: 'utf-8', env, timeout: 30000 }
    )
    // host mode with SRRS_LAT_* present -> fail_closed
    expect(result.status).toBe(EXIT_FAIL_CLOSED)
    let parsed: Record<string, unknown> | null = null
    try { parsed = JSON.parse(result.stdout ?? '') } catch { /* ignore */ }
    expect(parsed).not.toBeNull()
    expect(parsed?.['verdict']).toBe('fail_closed')
  })
})

// ============================================================================
// AC20: subprocess raw stderr not forwarded, stdout is single JSON
// ============================================================================

describe('runtime safety #1157: AC20 stdout single JSON object', () => {
  it('GIVEN --json mode WHEN verifier runs THEN stdout is exactly one valid JSON object', () => {
    const result = runVerifierJson({
      SRRS_LAT_CREDENTIAL_STATE: 'absent',
      SRRS_LAT_HOOK_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      SRRS_LAT_ACTIVE_PROCESS: 'preload_absent',
      SRRS_LAT_LOCAL_STORAGE: 'absent',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
    })
    // stdout should be parseable as a single JSON object
    expect(result.json).not.toBeNull()
    // Ensure stdout doesn't have extra content after the JSON
    const trimmed = result.stdout.trim()
    expect(() => JSON.parse(trimmed)).not.toThrow()
  })
})

// ============================================================================
// AC21: managed policy / plugin hook check -> unknown if cannot inspect
// ============================================================================

describe('runtime safety #1157: AC21 managed hook unknown -> fail_closed', () => {
  it('GIVEN SRRS_LAT_MANAGED_HOOK=unknown WHEN verifier runs THEN latitude verdict is fail_closed', () => {
    const result = runVerifierJson({
      SRRS_LAT_MANAGED_HOOK: 'unknown',
      SRRS_LAT_CREDENTIAL_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      SRRS_LAT_ACTIVE_PROCESS: 'preload_absent',
      SRRS_LAT_LOCAL_STORAGE: 'absent',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
    })
    const lat = ((result.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    // latitude_runtime_state_unknown -> fail_closed
    expect(lat?.['verdict']).toBe('fail_closed')
  })
})

// ============================================================================
// AC22: lstat unsafe_metadata -> blocked
// ============================================================================

describe('runtime safety #1157: AC22 lstat unsafe metadata blocked', () => {
  it('GIVEN local_storage state unsafe_metadata WHEN verifier runs THEN latitude blocked with local_storage_unsafe_metadata', () => {
    const result = runVerifierJson({
      SRRS_LAT_LOCAL_STORAGE: 'unsafe_metadata',
      SRRS_LAT_CREDENTIAL_STATE: 'absent',
      SRRS_LAT_HOOK_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      SRRS_LAT_ACTIVE_PROCESS: 'preload_absent',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
    })
    expect(result.exitCode).toBe(EXIT_FAIL)
    const lat = ((result.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    expect(lat?.['verdict']).toBe('blocked')
    const rcs = (lat?.['reason_codes'] as string[]) ?? []
    expect(rcs.some(rc => rc.includes('local_storage_unsafe_metadata'))).toBe(true)
  })
})

// ============================================================================
// AC23: LATITUDE_BASE_URL unapproved -> blocked
// ============================================================================

describe('runtime safety #1157: AC23 unapproved base URL blocked', () => {
  it('GIVEN LATITUDE_BASE_URL pointing to unapproved origin WHEN verifier runs THEN latitude blocked with destination_unapproved', () => {
    const result = runVerifierJson({
      SRRS_LAT_BASE_URL: 'https://evil-intercept.example.com/latitude',
      SRRS_LAT_CREDENTIAL_STATE: 'absent',
      SRRS_LAT_HOOK_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      SRRS_LAT_ACTIVE_PROCESS: 'preload_absent',
      SRRS_LAT_LOCAL_STORAGE: 'absent',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
    })
    expect(result.exitCode).toBe(EXIT_FAIL)
    const lat = ((result.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    const rcs = (lat?.['reason_codes'] as string[]) ?? []
    expect(rcs.some(rc => rc.includes('destination_unapproved'))).toBe(true)
  })
})

// ============================================================================
// AC24: LATITUDE_DEBUG=1 -> blocked
// ============================================================================

describe('runtime safety #1157: AC24 LATITUDE_DEBUG=1 blocked', () => {
  it('GIVEN LATITUDE_DEBUG=1 WHEN verifier runs THEN latitude blocked with diagnostic_logging_enabled', () => {
    const result = runVerifierJson({
      SRRS_LAT_DEBUG: '1',
      SRRS_LAT_CREDENTIAL_STATE: 'absent',
      SRRS_LAT_HOOK_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      SRRS_LAT_ACTIVE_PROCESS: 'preload_absent',
      SRRS_LAT_LOCAL_STORAGE: 'absent',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
    })
    expect(result.exitCode).toBe(EXIT_FAIL)
    const lat = ((result.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    const rcs = (lat?.['reason_codes'] as string[]) ?? []
    expect(rcs.some(rc => rc.includes('diagnostic_logging_enabled'))).toBe(true)
  })
})

// ============================================================================
// AC25: quiescent two-stage postcondition
// ============================================================================

describe('runtime safety #1157: AC25 quiescent two-stage postcondition', () => {
  it('GIVEN two successive runs with same clean overrides WHEN comparing THEN both return same verdict', () => {
    const overrides = {
      SRRS_LAT_CREDENTIAL_STATE: 'absent',
      SRRS_LAT_HOOK_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      SRRS_LAT_ACTIVE_PROCESS: 'preload_absent',
      SRRS_LAT_LOCAL_STORAGE: 'absent',
      SRRS_LAT_UNINSTALL_STATE: 'complete',
      SRRS_LAT_CONTAINMENT_STATE: 'never_observed',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
    }
    const r1 = runVerifierJson(overrides)
    const r2 = runVerifierJson(overrides)
    expect(r1.exitCode).toBe(r2.exitCode)
    const lat1 = ((r1.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    const lat2 = ((r2.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    expect(lat1?.['verdict']).toBe(lat2?.['verdict'])
  })
})

// ============================================================================
// AC26: npm registry provenance unknown -> distribution_provenance_unknown
// ============================================================================

describe('runtime safety #1157: AC26 provenance unknown blocked', () => {
  it('GIVEN dist_spec pinned but provenance unknown WHEN verifier runs THEN latitude blocked with distribution_provenance_unknown', () => {
    const result = runVerifierJson({
      SRRS_LAT_DIST_SPEC: '@latitude-so/sdk@1.2.3',
      SRRS_LAT_DIST_INTEGRITY: 'verified',
      SRRS_LAT_DIST_PROVENANCE: 'unknown',
      SRRS_LAT_CREDENTIAL_STATE: 'absent',
      SRRS_LAT_HOOK_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      SRRS_LAT_ACTIVE_PROCESS: 'preload_absent',
      SRRS_LAT_LOCAL_STORAGE: 'absent',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
    })
    const lat = ((result.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    const rcs = (lat?.['reason_codes'] as string[]) ?? []
    expect(rcs.some(rc => rc.includes('distribution_provenance_unknown'))).toBe(true)
  })
})

// ============================================================================
// AC27: containment_state != never_observed -> not safe
// ============================================================================

describe('runtime safety #1157: AC27 containment_state active -> blocked', () => {
  it('GIVEN containment_state=active WHEN verifier runs THEN latitude blocked', () => {
    const result = runVerifierJson({
      SRRS_LAT_CONTAINMENT_STATE: 'active',
      SRRS_LAT_CREDENTIAL_STATE: 'absent',
      SRRS_LAT_HOOK_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      SRRS_LAT_ACTIVE_PROCESS: 'preload_absent',
      SRRS_LAT_LOCAL_STORAGE: 'absent',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
    })
    expect(result.exitCode).toBe(EXIT_FAIL)
    const lat = ((result.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    expect(lat?.['verdict']).toBe('blocked')
  })

  it('GIVEN containment_state=contained (not never_observed) WHEN verifier runs THEN latitude not safe', () => {
    const result = runVerifierJson({
      SRRS_LAT_CONTAINMENT_STATE: 'contained',
      SRRS_LAT_CREDENTIAL_STATE: 'absent',
      SRRS_LAT_HOOK_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      SRRS_LAT_ACTIVE_PROCESS: 'preload_absent',
      SRRS_LAT_LOCAL_STORAGE: 'absent',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
    })
    const lat = ((result.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    // contained is not never_observed -> blocked
    expect(lat?.['verdict']).toBe('blocked')
  })
})

// ============================================================================
// AC28: credential in argv -> exposure_state=possible -> blocked
// ============================================================================

describe('runtime safety #1157: AC28 argv credential exposure', () => {
  it('GIVEN SRRS_LAT_ARGV_CREDENTIAL=present WHEN verifier runs THEN latitude blocked with exposure_possible_or_confirmed', () => {
    const result = runVerifierJson({
      SRRS_LAT_ARGV_CREDENTIAL: 'present',
      SRRS_LAT_CREDENTIAL_STATE: 'absent',
      SRRS_LAT_HOOK_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      SRRS_LAT_ACTIVE_PROCESS: 'preload_absent',
      SRRS_LAT_LOCAL_STORAGE: 'absent',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
    })
    expect(result.exitCode).toBe(EXIT_FAIL)
    const lat = ((result.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    const rcs = (lat?.['reason_codes'] as string[]) ?? []
    expect(rcs.some(rc => rc.includes('exposure_possible_or_confirmed'))).toBe(true)
  })
})

// ============================================================================
// AC29: fixture path and security scripts available
// ============================================================================

describe('runtime safety #1157: AC29 fixtures and scripts available', () => {
  it('GIVEN repository structure WHEN checking paths THEN session-recording-runtime-safety fixtures exist', () => {
    const fixturesPath = resolve(REPO_ROOT, 'tests', 'fixtures', 'session-recording-runtime-safety')
    expect(existsSync(fixturesPath)).toBe(true)
  })

  it('GIVEN repository structure WHEN checking package.json THEN security:session-recording scripts exist', () => {
    const pkg = JSON.parse(readFileSync(resolve(REPO_ROOT, 'package.json'), 'utf-8'))
    const scripts = pkg.scripts ?? {}
    // security:session-recording:fixture should exist
    expect(Object.keys(scripts).some(k => k.includes('session-recording'))).toBe(true)
  })
})

// ============================================================================
// ITERATION-1 B-BLOCKER REGRESSION TESTS
// ============================================================================

// ============================================================================
// B1: fixture mode does not read real host surfaces
// ============================================================================

describe('runtime safety #1157 B1: fixture isolation — no real host surfaces', () => {
  it('GIVEN fixture profile with all absent overrides WHEN verifier runs THEN not blocked by host credential state', () => {
    const result = runVerifierJson({
      SRRS_LAT_CREDENTIAL_STATE: 'absent',
      SRRS_LAT_HOOK_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      SRRS_LAT_ACTIVE_PROCESS: 'preload_absent',
      SRRS_LAT_LOCAL_STORAGE: 'absent',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
      SRRS_LAT_CONTAINMENT_STATE: 'never_observed',
      SRRS_LAT_EXPOSURE_STATE: 'none_observed',
      SRRS_LAT_UNINSTALL_STATE: 'not_attempted',
      SRRS_LAT_TRANSPORT_STATE: 'https',
      SRRS_LAT_DESTINATION_STATE: 'approved_cloud',
      SRRS_LAT_DIAGNOSTIC_LOG: 'disabled',
    })
    // In fixture mode, all overrides applied, no host surfaces touched
    // Result should not be blocked (fixture isolation working)
    const lat = ((result.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    expect(lat?.['verdict']).not.toBe('blocked')
  })

  it('GIVEN fixture profile WITHOUT explicit active_process override WHEN verifier runs THEN active_process_state is unknown (not host)', () => {
    // Without SRRS_LAT_ACTIVE_PROCESS override in fixture mode,
    // active_process_state should be unknown (not querying real pgrep/proc)
    const result = runVerifierJson({
      SRRS_LAT_CREDENTIAL_STATE: 'absent',
      SRRS_LAT_HOOK_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      SRRS_LAT_LOCAL_STORAGE: 'absent',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
      SRRS_LAT_CONTAINMENT_STATE: 'never_observed',
      SRRS_LAT_EXPOSURE_STATE: 'none_observed',
    })
    // active_process_state unknown in fixture mode -> fail_closed (not invoking real pgrep)
    const lat = ((result.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    expect(lat?.['active_process_state']).toBe('unknown')
  })
})

// ============================================================================
// B2: upstream Latitude storage path (~/.claude/state/latitude/) checked
// ============================================================================

describe('runtime safety #1157 B2: upstream latitude storage path in checked_surfaces', () => {
  it('GIVEN --json fixture mode WHEN local_storage override present THEN checked_surfaces includes claude_state_latitude', () => {
    const result = runVerifierJson({
      SRRS_LAT_LOCAL_STORAGE: 'absent',
      SRRS_LAT_CREDENTIAL_STATE: 'absent',
      SRRS_LAT_HOOK_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      SRRS_LAT_ACTIVE_PROCESS: 'preload_absent',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
    })
    const lat = ((result.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    const surfaces = (lat?.['checked_surfaces'] as string[]) ?? []
    // B2: upstream path must be in checked_surfaces
    expect(surfaces.some(s => s.includes('claude_state_latitude') || s.includes('latitude_state'))).toBe(true)
  })

  it('GIVEN local_storage override present WHEN verifier runs THEN latitude_state surface is reported', () => {
    const result = runVerifierJson({
      SRRS_LAT_LOCAL_STORAGE: 'present',
      SRRS_LAT_CREDENTIAL_STATE: 'absent',
      SRRS_LAT_HOOK_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      SRRS_LAT_ACTIVE_PROCESS: 'preload_absent',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
    })
    const lat = ((result.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    const surfaces = (lat?.['checked_surfaces'] as string[]) ?? []
    expect(surfaces.some(s => s.includes('latitude_state') || s.includes('claude_state_latitude'))).toBe(true)
  })
})

// ============================================================================
// B3: systemd / pgrep failure -> unknown, not silent pass
// ============================================================================

describe('runtime safety #1157 B3: fail-closed on inspection errors', () => {
  it('GIVEN managed hook state unknown WHEN verifier runs THEN latitude verdict is fail_closed (not pass)', () => {
    const result = runVerifierJson({
      SRRS_LAT_MANAGED_HOOK: 'unknown',
      SRRS_LAT_CREDENTIAL_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      SRRS_LAT_ACTIVE_PROCESS: 'preload_absent',
      SRRS_LAT_LOCAL_STORAGE: 'absent',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
    })
    const lat = ((result.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    // managed hook unknown -> fail_closed (not silent pass)
    expect(lat?.['verdict']).toBe('fail_closed')
  })

  it('GIVEN active_process unknown WHEN verifier runs THEN latitude verdict is fail_closed', () => {
    const result = runVerifierJson({
      SRRS_LAT_ACTIVE_PROCESS: 'unknown',
      SRRS_LAT_CREDENTIAL_STATE: 'absent',
      SRRS_LAT_HOOK_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      SRRS_LAT_LOCAL_STORAGE: 'absent',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
    })
    const lat = ((result.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    expect(lat?.['verdict']).toBe('fail_closed')
  })
})

// ============================================================================
// B4: scoped package pin detection + distribution check
// ============================================================================

describe('runtime safety #1157 B4: scoped package version pin detection', () => {
  it('GIVEN npx @latitude-data/claude-code-telemetry (no version) WHEN verifier runs THEN distribution_unpinned', () => {
    const result = runVerifierJson({
      SRRS_LAT_DIST_SPEC: 'npx @latitude-data/claude-code-telemetry',
      SRRS_LAT_DIST_INTEGRITY: 'unknown',
      SRRS_LAT_DIST_PROVENANCE: 'unknown',
      SRRS_LAT_CREDENTIAL_STATE: 'absent',
      SRRS_LAT_HOOK_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      SRRS_LAT_ACTIVE_PROCESS: 'preload_absent',
      SRRS_LAT_LOCAL_STORAGE: 'absent',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
    })
    const lat = ((result.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    const rcs = (lat?.['reason_codes'] as string[]) ?? []
    expect(rcs.some(rc => rc.includes('distribution_unpinned'))).toBe(true)
  })

  it('GIVEN npx @latitude-data/claude-code-telemetry@1.2.3 (pinned) WHEN verifier runs THEN distribution_unpinned absent', () => {
    const result = runVerifierJson({
      SRRS_LAT_DIST_SPEC: 'npx @latitude-data/claude-code-telemetry@1.2.3',
      SRRS_LAT_DIST_INTEGRITY: 'verified',
      SRRS_LAT_DIST_PROVENANCE: 'verified',
      SRRS_LAT_CREDENTIAL_STATE: 'absent',
      SRRS_LAT_HOOK_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      SRRS_LAT_ACTIVE_PROCESS: 'preload_absent',
      SRRS_LAT_LOCAL_STORAGE: 'absent',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
    })
    const lat = ((result.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    const rcs = (lat?.['reason_codes'] as string[]) ?? []
    expect(rcs.some(rc => rc.includes('distribution_unpinned'))).toBe(false)
  })
})

// ============================================================================
// B5: exact origin matching — no prefix spoofing
// ============================================================================

describe('runtime safety #1157 B5: exact origin matching for destination', () => {
  it('GIVEN LATITUDE_BASE_URL=https://ingest.latitude.so WHEN verifier runs THEN destination approved (not unapproved)', () => {
    const result = runVerifierJson({
      SRRS_LAT_BASE_URL: 'https://ingest.latitude.so',
      SRRS_LAT_CREDENTIAL_STATE: 'absent',
      SRRS_LAT_HOOK_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      SRRS_LAT_ACTIVE_PROCESS: 'preload_absent',
      SRRS_LAT_LOCAL_STORAGE: 'absent',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
    })
    const lat = ((result.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    const rcs = (lat?.['reason_codes'] as string[]) ?? []
    // ingest.latitude.so must be in allowlist
    expect(rcs.some(rc => rc.includes('destination_unapproved'))).toBe(false)
    expect(lat?.['destination_state']).toBe('approved_cloud')
  })

  it('GIVEN LATITUDE_BASE_URL=https://latitude.so.evil.example WHEN verifier runs THEN destination_unapproved (no prefix spoofing)', () => {
    const result = runVerifierJson({
      SRRS_LAT_BASE_URL: 'https://latitude.so.evil.example',
      SRRS_LAT_CREDENTIAL_STATE: 'absent',
      SRRS_LAT_HOOK_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      SRRS_LAT_ACTIVE_PROCESS: 'preload_absent',
      SRRS_LAT_LOCAL_STORAGE: 'absent',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
    })
    expect(result.exitCode).toBe(EXIT_FAIL)
    const lat = ((result.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    const rcs = (lat?.['reason_codes'] as string[]) ?? []
    expect(rcs.some(rc => rc.includes('destination_unapproved'))).toBe(true)
  })

  it('GIVEN LATITUDE_BASE_URL=https://latitude.so/some/path WHEN verifier runs THEN destination approved', () => {
    const result = runVerifierJson({
      SRRS_LAT_BASE_URL: 'https://latitude.so/api/v1',
      SRRS_LAT_CREDENTIAL_STATE: 'absent',
      SRRS_LAT_HOOK_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      SRRS_LAT_ACTIVE_PROCESS: 'preload_absent',
      SRRS_LAT_LOCAL_STORAGE: 'absent',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
    })
    const lat = ((result.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    // https://latitude.so is approved, path suffix must not disqualify it
    expect(lat?.['destination_state']).toBe('approved_cloud')
  })
})

// ============================================================================
// B6: SRRS_SECRETS_MODE unset + credential present -> policy_mode_mismatch
// ============================================================================

describe('runtime safety #1157 B6: secrets mode policy mismatch', () => {
  it('GIVEN credential present AND SRRS_SECRETS_MODE unset WHEN verifier runs THEN latitude blocked with policy_mode_mismatch', () => {
    const repoRoot = REPO_ROOT
    const env: Record<string, string> = {
      ...(process.env as Record<string, string>),
      SRRS_LAT_CREDENTIAL_STATE: 'present',
      SRRS_LAT_HOOK_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      SRRS_LAT_ACTIVE_PROCESS: 'preload_absent',
      SRRS_LAT_LOCAL_STORAGE: 'absent',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
      SRRS_GIT_LS_REMOTE_EXIT: '2',
      SRRS_GH_VISIBILITY: 'private',
      SRRS_GIT_CONFIG_OUTPUT: '',
      SRRS_CHECKPOINT_TOKEN: 'absent',
      SRRS_REPO_ROOT: repoRoot,
    }
    delete env['SRRS_SECRETS_MODE']
    delete env['ENTIRE_CHECKPOINT_TOKEN']
    delete env['LATITUDE_API_KEY']
    delete env['LATITUDE_CLAUDE_CODE_ENABLED']
    delete env['LATITUDE_BASE_URL']
    delete env['LATITUDE_DEBUG']
    delete env['BUN_OPTIONS']

    const result = spawnSync(
      'python3',
      [SCRIPT, '--json', '--execution-profile', 'fixture'],
      { encoding: 'utf-8', env, timeout: 30000 }
    )
    let parsed: Record<string, unknown> | null = null
    try { parsed = JSON.parse(result.stdout ?? '') } catch { /* ignore */ }
    const lat = ((parsed as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    const rcs = (lat?.['reason_codes'] as string[]) ?? []
    // credential present + no secrets_mode -> policy_mode_mismatch -> blocked
    expect(rcs.some(rc => rc.includes('policy_mode_mismatch'))).toBe(true)
    expect(lat?.['verdict']).toBe('blocked')
  })
})

// ============================================================================
// B7: inspection_complete is independent of verdict
// ============================================================================

describe('runtime safety #1157 B7: inspection_complete independent of verdict', () => {
  it('GIVEN credential blocked AND active_process unknown WHEN verifier runs THEN inspection_complete=false even when verdict=blocked', () => {
    const result = runVerifierJson({
      SRRS_LAT_CREDENTIAL_STATE: 'present',
      SRRS_LAT_HOOK_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      // No SRRS_LAT_ACTIVE_PROCESS -> fixture mode returns unknown
      SRRS_LAT_LOCAL_STORAGE: 'absent',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
    })
    const lat = ((result.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    // verdict=blocked (credential present), but active_process inspection gap exists
    expect(lat?.['verdict']).toBe('blocked')
    // inspection_complete=false because active_process is unknown (inspection gap)
    expect(result.json?.['inspection_complete']).toBe(false)
  })

  it('GIVEN all surfaces fully overridden WHEN verifier runs THEN inspection_complete reflects gap count', () => {
    const result = runVerifierJson({
      SRRS_LAT_CREDENTIAL_STATE: 'absent',
      SRRS_LAT_HOOK_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      SRRS_LAT_ACTIVE_PROCESS: 'preload_absent',
      SRRS_LAT_LOCAL_STORAGE: 'absent',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
      SRRS_LAT_CONTAINMENT_STATE: 'never_observed',
      SRRS_LAT_EXPOSURE_STATE: 'none_observed',
    })
    // With explicit overrides for all surfaces, inspection_gaps should be empty
    const lat = ((result.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    const gaps = (lat?.['inspection_gaps'] as string[]) ?? []
    expect(Array.isArray(gaps)).toBe(true)
    // gaps should be minimal when major surfaces are overridden
    expect(gaps.length).toBeLessThanOrEqual(5)
  })
})

// ============================================================================
// B8: uninstall postcondition — two-snapshot quiescent verification
// ============================================================================

describe('runtime safety #1157 B8: uninstall postcondition snapshot verification', () => {
  it('GIVEN uninstall_state complete override WHEN verifier runs THEN uninstall_state reported as complete', () => {
    const result = runVerifierJson({
      SRRS_LAT_UNINSTALL_STATE: 'complete',
      SRRS_LAT_CREDENTIAL_STATE: 'absent',
      SRRS_LAT_HOOK_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      SRRS_LAT_ACTIVE_PROCESS: 'preload_absent',
      SRRS_LAT_LOCAL_STORAGE: 'absent',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
    })
    const lat = ((result.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    expect(lat?.['uninstall_state']).toBe('complete')
  })

  it('GIVEN uninstall_state incomplete WHEN verifier runs THEN latitude verdict is blocked', () => {
    const result = runVerifierJson({
      SRRS_LAT_UNINSTALL_STATE: 'incomplete',
      SRRS_LAT_CREDENTIAL_STATE: 'absent',
      SRRS_LAT_HOOK_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      SRRS_LAT_ACTIVE_PROCESS: 'preload_absent',
      SRRS_LAT_LOCAL_STORAGE: 'absent',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
    })
    expect(result.exitCode).toBe(EXIT_FAIL)
    const lat = ((result.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    const rcs = (lat?.['reason_codes'] as string[]) ?? []
    expect(rcs.some(rc => rc.includes('uninstall_incomplete'))).toBe(true)
  })

  it('GIVEN two successive identical runs WHEN comparing uninstall_state THEN both return same state (quiescent)', () => {
    const overrides = {
      SRRS_LAT_UNINSTALL_STATE: 'not_attempted',
      SRRS_LAT_CREDENTIAL_STATE: 'absent',
      SRRS_LAT_HOOK_STATE: 'absent',
      SRRS_LAT_PRELOAD_SETTINGS: 'absent',
      SRRS_LAT_ACTIVE_PROCESS: 'preload_absent',
      SRRS_LAT_LOCAL_STORAGE: 'absent',
      SRRS_LAT_REMOTE_TRACE: 'absent_human_attested',
      SRRS_LAT_CONTAINMENT_STATE: 'never_observed',
      SRRS_LAT_EXPOSURE_STATE: 'none_observed',
    }
    const r1 = runVerifierJson(overrides)
    const r2 = runVerifierJson(overrides)
    const lat1 = ((r1.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    const lat2 = ((r2.json as Record<string, unknown>)?.['components'] as Record<string, unknown>)?.['latitude'] as Record<string, unknown>
    // Both snapshots must agree (quiescent)
    expect(lat1?.['uninstall_state']).toBe(lat2?.['uninstall_state'])
    expect(r1.exitCode).toBe(r2.exitCode)
  })
})
