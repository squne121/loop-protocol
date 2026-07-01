import { spawnSync } from 'node:child_process'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

const __filename = fileURLToPath(import.meta.url)
const REPO_ROOT = resolve(dirname(__filename), '..', '..')
const SCRIPT_PATH = resolve(REPO_ROOT, 'scripts/validate-roadmap-refs.mjs')
const FIXTURE_DIR = resolve(REPO_ROOT, 'tests/fixtures/roadmap-refs')

function runValidator(args = []) {
  return spawnSync(process.execPath, [SCRIPT_PATH, ...args], {
    cwd: REPO_ROOT,
    encoding: 'utf-8',
    stdio: ['pipe', 'pipe', 'pipe'],
  })
}

describe('validate roadmap fenced yaml references', () => {
  it('GIVEN valid fixture WHEN validator runs THEN exits 0', () => {
    const result = runValidator(['--file', resolve(FIXTURE_DIR, 'valid.md')])

    expect(result.status).toBe(0)
    expect(result.stdout).toContain('[OK]')
  })

  it('GIVEN malformed fixture WHEN validator runs THEN exits non-zero', () => {
    const result = runValidator(['--file', resolve(FIXTURE_DIR, 'malformed-yaml.md')])

    expect(result.status).not.toBe(0)
    expect(result.stderr).toContain('[FAIL] block#1 invalid YAML')
  })

  it('GIVEN duplicate-key fixture WHEN validator runs THEN exits non-zero', () => {
    const result = runValidator(['--file', resolve(FIXTURE_DIR, 'duplicate-key.md')])

    expect(result.status).not.toBe(0)
    expect(result.stderr).toContain('[FAIL] block#1 invalid YAML')
  })

  it('GIVEN missing-target fixture WHEN validator runs THEN exits non-zero', () => {
    const result = runValidator(['--file', resolve(FIXTURE_DIR, 'missing-target.md')])

    expect(result.status).not.toBe(0)
    expect(result.stderr).toContain('target does not exist')
  })

  it('GIVEN root-escape fixture WHEN validator runs THEN exits non-zero', () => {
    const result = runValidator(['--file', resolve(FIXTURE_DIR, 'root-escape.md')])

    expect(result.status).not.toBe(0)
    expect(result.stderr).toContain('path traversal is not allowed')
  })

  it('GIVEN stale-alias fixture WHEN validator runs THEN exits non-zero', () => {
    const result = runValidator(['--file', resolve(FIXTURE_DIR, 'stale-alias.md')])

    expect(result.status).not.toBe(0)
    expect(result.stderr).toContain('stale alias is denied')
  })

  it('GIVEN no flags WHEN validator runs THEN validates default roadmap file', () => {
    const result = runValidator([])

    expect(result.status).not.toBeNull()
    const output = `${result.stdout}${result.stderr}`
    expect(output).toContain('roadmap')
  })
})
