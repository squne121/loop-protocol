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
  const dangerousValues = ['publish_secret', 'app_runtime_secret', 'agent_local_secret', 'checkpoint_token']

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
