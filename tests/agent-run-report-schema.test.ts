import { readFileSync } from 'fs'
import { resolve } from 'path'
import { describe, expect, it } from 'vitest'
import { createValidReport, createValidRetroIndex, REPO_ROOT } from './agent-run-report-test-helpers'
import {
  validateReportAgainstSchema,
  validateRetroIndexAgainstSchema,
} from '../scripts/lib/agent-run-report-validation.mjs'

function readReportFixture(name: string) {
  return JSON.parse(readFileSync(resolve(REPO_ROOT, 'tests/fixtures/agent-run-report', name), 'utf-8'))
}

describe('agent_run_report schema compile', () => {
  it('GIVEN agent_run_report schema WHEN compiled with Ajv 2020-12 THEN it validates a valid report', () => {
    const result = validateReportAgainstSchema(createValidReport())
    expect(result.valid).toBe(true)
  })

  it('GIVEN a safe entirecli fixture WHEN compiled with Ajv 2020-12 THEN public report admission stays valid', () => {
    const result = validateReportAgainstSchema(readReportFixture('valid-public-entirecli-safe.json'))
    expect(result.valid).toBe(true)
  })

  it('GIVEN a not_applicable entirecli fixture WHEN compiled with Ajv 2020-12 THEN public report admission stays valid', () => {
    const result = validateReportAgainstSchema(readReportFixture('valid-public-entirecli-not-applicable.json'))
    expect(result.valid).toBe(true)
  })

  it('GIVEN an entirecli fixture missing a required field WHEN compiled with Ajv 2020-12 THEN validation fails', () => {
    const result = validateReportAgainstSchema(readReportFixture('invalid-public-entirecli-missing-field.json'))
    expect(result.valid).toBe(false)
  })

  it('GIVEN an entirecli fixture with an unknown key WHEN compiled with Ajv 2020-12 THEN validation fails', () => {
    const result = validateReportAgainstSchema(readReportFixture('invalid-public-entirecli-unknown-key.json'))
    expect(result.valid).toBe(false)
  })

  it('GIVEN an entirecli fixture with an unknown verdict WHEN compiled with Ajv 2020-12 THEN validation fails', () => {
    const result = validateReportAgainstSchema(readReportFixture('invalid-public-entirecli-unknown-verdict.json'))
    expect(result.valid).toBe(false)
  })

  it('GIVEN an entirecli fixture with a bad schema_version WHEN compiled with Ajv 2020-12 THEN validation fails', () => {
    const result = validateReportAgainstSchema(readReportFixture('invalid-public-entirecli-bad-schema-version.json'))
    expect(result.valid).toBe(false)
  })

  it('GIVEN a blocked entirecli fixture on a public surface WHEN compiled with Ajv 2020-12 THEN validation fails', () => {
    const result = validateReportAgainstSchema(readReportFixture('invalid-public-entirecli-blocked.json'))
    expect(result.valid).toBe(false)
  })

  it('GIVEN a public report with raw entirecli values WHEN compiled with Ajv 2020-12 THEN validation fails', () => {
    const result = validateReportAgainstSchema(readReportFixture('invalid-public-entirecli-raw-values.json'))
    expect(result.valid).toBe(false)
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
