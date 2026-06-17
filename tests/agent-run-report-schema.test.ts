import { readFileSync } from 'fs'
import { resolve } from 'path'
import { describe, expect, it } from 'vitest'
import { createValidReport, createValidRetroIndex, REPO_ROOT } from './agent-run-report-test-helpers'
import {
  validateReportAgainstSchema,
  validateRetroIndexAgainstSchema,
} from '../scripts/lib/agent-run-report-validation.mjs'

describe('agent_run_report schema compile', () => {
  it('GIVEN agent_run_report schema WHEN compiled with Ajv 2020-12 THEN it validates a valid report', () => {
    const result = validateReportAgainstSchema(createValidReport())
    expect(result.valid).toBe(true)
  })

  it('GIVEN agent_retro_index schema WHEN compiled with Ajv 2020-12 THEN it validates a valid index', () => {
    const result = validateRetroIndexAgainstSchema(createValidRetroIndex())
    expect(result.valid).toBe(true)
  })

  it('GIVEN schema files WHEN loaded THEN they declare draft 2020-12', () => {
    const reportSchema = JSON.parse(readFileSync(resolve(REPO_ROOT, 'docs/schemas/agent-run-report.schema.json'), 'utf-8'))
    const retroSchema = JSON.parse(readFileSync(resolve(REPO_ROOT, 'docs/schemas/agent-retro-index.schema.json'), 'utf-8'))
    expect(reportSchema.$schema).toBe('https://json-schema.org/draft/2020-12/schema')
    expect(retroSchema.$schema).toBe('https://json-schema.org/draft/2020-12/schema')
  })
})
