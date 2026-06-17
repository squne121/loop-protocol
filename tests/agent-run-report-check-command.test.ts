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

  it('GIVEN explicit target matching 0 files WHEN check command is run THEN exits 1', () => {
    const result = runAgentRunReportCheck(['tests/fixtures/agent-run-report/does-not-exist/*.json'])
    expect(result.exitCode).toBe(1)
    expect(result.stderr).toContain('no files found')
  })

  it('GIVEN default target override that matches 0 files in CI WHEN check command is run THEN exits 1', () => {
    const result = runAgentRunReportCheck(['--require-target'], {
      CI: 'true',
    })
    expect(result.exitCode).toBe(1)
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
