import { spawnSync } from 'node:child_process'
import { dirname, resolve } from 'node:path'
import process from 'node:process'
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

function runNpmValidate(args = []) {
  return spawnSync('pnpm', ['--silent', 'run', 'validate:roadmap-refs', ...args], {
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

  it('GIVEN positional argument WHEN validator runs THEN exits non-zero as unknown arg', () => {
    const result = runValidator(['unexpected'])

    expect(result.status).toBe(2)
    expect(result.stderr).toContain('unknown argument: unexpected')
  })

  it('GIVEN default target file WHEN node command runs THEN exits 0 with success output and no stderr', () => {
    const result = runValidator([])

    expect(result.status).toBe(0)
    expect(result.stdout).toContain('[OK] roadmap fenced YAML reference checks passed')
    expect(result.stdout).toContain('not checked: product-spec lifecycle status')
    expect(result.stderr).toBe('')
  })

  it('GIVEN default command via pnpm script WHEN validator runs THEN exits 0 with success output and no stderr', () => {
    const result = runNpmValidate()

    expect(result.status).toBe(0)
    expect(result.stdout).toContain('[OK] roadmap fenced YAML reference checks passed')
    expect(result.stdout).toContain('not checked: product-spec lifecycle status')
    expect(result.stderr).toBe('')
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

  it('GIVEN absolute path fixture WHEN validator runs THEN exits non-zero', () => {
    const result = runValidator(['--file', resolve(FIXTURE_DIR, 'absolute-path.md')])

    expect(result.status).not.toBe(0)
    expect(result.stderr).toContain('absolute path is not allowed')
  })

  it('GIVEN directory fixture WHEN validator runs THEN exits non-zero', () => {
    const result = runValidator(['--file', resolve(FIXTURE_DIR, 'directory.md')])

    expect(result.status).not.toBe(0)
    expect(result.stderr).toContain('must point to a regular file')
  })

  it('GIVEN URL fixture WHEN validator runs THEN exits non-zero', () => {
    const result = runValidator(['--file', resolve(FIXTURE_DIR, 'url.md')])

    expect(result.status).not.toBe(0)
    expect(result.stderr).toContain('URL is not allowed')
  })

  it('GIVEN fragment fixture WHEN validator runs THEN exits non-zero', () => {
    const result = runValidator(['--file', resolve(FIXTURE_DIR, 'fragment.md')])

    expect(result.status).not.toBe(0)
    expect(result.stderr).toContain('URL fragment is not allowed in path')
  })

  it('GIVEN duplicate destination fixture WHEN validator runs THEN exits non-zero', () => {
    const result = runValidator(['--file', resolve(FIXTURE_DIR, 'duplicate-destination.md')])

    expect(result.status).not.toBe(0)
    expect(result.stderr).toContain('duplicate item in spec_destination')
  })

  it('GIVEN empty description fixture WHEN validator runs THEN exits non-zero', () => {
    const result = runValidator(['--file', resolve(FIXTURE_DIR, 'empty-description.md')])

    expect(result.status).not.toBe(0)
    expect(result.stderr).toContain('empty destination description')
  })
})
