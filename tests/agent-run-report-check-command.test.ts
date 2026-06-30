import { describe, expect, it } from 'vitest'
import { resolve } from 'path'
import {
  createOutsideRepoReportFixture,
  REPORT_FIXTURES_DIR,
  RETRO_FIXTURES_DIR,
  runAgentRunReportCheck,
} from './agent-run-report-test-helpers'

describe('agent-run-report:check entrypoint', () => {
  it('GIVEN valid explicit fixtures WHEN check command is run THEN exits 0', () => {
    const result = runAgentRunReportCheck([
      resolve(REPORT_FIXTURES_DIR, 'valid-basic.json'),
      resolve(RETRO_FIXTURES_DIR, 'valid-basic.json'),
    ])
    expect(result.exitCode).toBe(0)
    expect(result.stdout).toContain('PASS')
  })

  it('GIVEN explicit target matching 0 files WHEN check command is run THEN exits 1 with no-files message', () => {
    const result = runAgentRunReportCheck(['tests/fixtures/agent-run-report/does-not-exist/*.json'])
    expect(result.exitCode).toBe(1)
    expect(result.stderr).toContain('agent-run-report:check: no files found')
    expect(result.signal).toBeNull()
    expect(result.timedOut).toBe(false)
  })

  it('GIVEN default target override that matches 0 files in CI WHEN check command is run THEN exits 1 with no-files stderr', () => {
    const result = runAgentRunReportCheck(['--require-target'], {
      CI: 'true',
      AGENT_RUN_REPORT_CHECK_DEFAULT_PATTERNS: 'tests/fixtures/agent-run-report/does-not-exist/*.json',
    })
    expect(result.exitCode).toBe(1)
    expect(result.stderr).toContain('agent-run-report:check: no files found')
    expect(result.signal).toBeNull()
    expect(result.timedOut).toBe(false)
    expect(result.stdout).not.toContain('PASS')
  })

  it('GIVEN default target override matching 0 files outside CI WHEN check command is run THEN exits 0 with skip message', () => {
    const result = runAgentRunReportCheck([], {
      AGENT_RUN_REPORT_CHECK_DEFAULT_PATTERNS: 'tests/fixtures/agent-run-report/does-not-exist/*.json',
      CI: 'false',
    })
    expect(result.exitCode).toBe(0)
    expect(result.stdout).toContain('agent-run-report:check: no files found (default targets) - skipped')
    expect(result.signal).toBeNull()
    expect(result.timedOut).toBe(false)
  })

  it('GIVEN unknown option WHEN check command is run THEN exits 2', () => {
    const result = runAgentRunReportCheck(['--unknown-option'])
    expect(result.exitCode).toBe(2)
    expect(result.stderr).toContain('unknown option')
  })

  it('GIVEN repo-outside absolute target WHEN check command is run THEN exits 1 fail-closed', () => {
    const outsidePath = createOutsideRepoReportFixture()
    const result = runAgentRunReportCheck([outsidePath])
    expect(result.exitCode).toBe(1)
    expect(result.stderr).toContain('file.outside_repo')
  })
})
