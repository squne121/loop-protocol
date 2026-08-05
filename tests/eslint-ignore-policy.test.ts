/**
 * eslint-ignore-policy.test.ts
 *
 * Issue #1995: repo-approved local temporary workspace を tmp/ へ一本化し
 * .claude/tmp/ を非推奨化する。
 *
 * eslint.config.mjs の ignores 配列に '.claude/tmp/**' と 'tmp/**' が
 * 追加されたことを、実際に `pnpm lint` を子プロセスで実行して runtime に
 * 確認する（GIVEN/WHEN/THEN）。fixture はテスト内で生成・確実に削除する。
 *
 * Covers AC3, AC4, AC5 from Issue #1995 (Runtime Verification Applicability:
 * decision: immediate).
 */

import { execFileSync } from 'child_process'
import { mkdirSync, rmSync, writeFileSync } from 'fs'
import { resolve } from 'path'
import { afterEach, describe, expect, it } from 'vitest'

const REPO_ROOT = resolve(__dirname, '..')

// Invalid JS content guaranteed to produce an ESLint parse error if scanned.
const INVALID_JS_CONTENT = 'const x = {\n'

interface LintRunResult {
  stdout: string
  stderr: string
  exitCode: number
}

function runPnpmLint(): LintRunResult {
  try {
    const stdout = execFileSync('pnpm', ['lint'], {
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
      cwd: REPO_ROOT,
    })
    return { stdout, stderr: '', exitCode: 0 }
  } catch (error) {
    const err = error as { stdout?: string; stderr?: string; status?: number }
    return {
      stdout: err.stdout || '',
      stderr: err.stderr || '',
      exitCode: err.status ?? 1,
    }
  }
}

const fixturePathsToCleanup: string[] = []

afterEach(() => {
  while (fixturePathsToCleanup.length > 0) {
    const p = fixturePathsToCleanup.pop()
    if (p) {
      rmSync(p, { recursive: true, force: true })
    }
  }
})

describe('eslint-ignore-policy (Issue #1995)', () => {
  it(
    'GIVEN an invalid JS fixture under .claude/tmp/ WHEN pnpm lint runs THEN it is not scanned (claude/tmp fixture is not scanned by pnpm lint)',
    () => {
      const fixtureDir = resolve(REPO_ROOT, '.claude/tmp/__eslint_ignore_policy_fixture__')
      const fixtureFile = resolve(fixtureDir, 'invalid.js')
      fixturePathsToCleanup.push(fixtureDir)

      mkdirSync(fixtureDir, { recursive: true })
      writeFileSync(fixtureFile, INVALID_JS_CONTENT, 'utf-8')

      const result = runPnpmLint()

      expect(result.stdout + result.stderr).not.toContain('__eslint_ignore_policy_fixture__')
      expect(result.exitCode).toBe(0)
    },
    120_000
  )

  it(
    'GIVEN an invalid JS fixture under tmp/ WHEN pnpm lint runs THEN it is not scanned (tmp fixture is not scanned by pnpm lint)',
    () => {
      const fixtureDir = resolve(REPO_ROOT, 'tmp/__eslint_ignore_policy_fixture__')
      const fixtureFile = resolve(fixtureDir, 'invalid.js')
      fixturePathsToCleanup.push(fixtureDir)

      mkdirSync(fixtureDir, { recursive: true })
      writeFileSync(fixtureFile, INVALID_JS_CONTENT, 'utf-8')

      const result = runPnpmLint()

      expect(result.stdout + result.stderr).not.toContain('__eslint_ignore_policy_fixture__')
      expect(result.exitCode).toBe(0)
    },
    120_000
  )

  it(
    'GIVEN an invalid TS fixture under a normal source path WHEN pnpm lint runs THEN it is still scanned (negative control: normal source path fixture is still scanned by pnpm lint)',
    () => {
      const fixtureFile = resolve(REPO_ROOT, 'src/__eslint_ignore_policy_fixture__.ts')
      fixturePathsToCleanup.push(fixtureFile)

      writeFileSync(fixtureFile, INVALID_JS_CONTENT, 'utf-8')

      const result = runPnpmLint()

      expect(result.stdout + result.stderr).toContain('__eslint_ignore_policy_fixture__')
      expect(result.exitCode).not.toBe(0)
    },
    120_000
  )
})
