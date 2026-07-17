import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const packageJsonPath = path.resolve(__dirname, '../package.json')

interface PackageManifest {
  scripts?: Record<string, string>
}

function readTestScript(): string {
  const raw = readFileSync(packageJsonPath, 'utf-8')
  const manifest = JSON.parse(raw) as PackageManifest
  const script = manifest.scripts?.test
  if (typeof script !== 'string') {
    throw new Error('package.json scripts.test is missing or not a string')
  }
  return script
}

describe('vitest test discovery policy (package.json "test" script)', () => {
  it('GIVEN the package.json test script WHEN it is read THEN it excludes repo-approved temporary workspace .claude/tmp/**', () => {
    const script = readTestScript()

    expect(script).toContain("--exclude '.claude/tmp/**'")
  })

  it('GIVEN the package.json test script WHEN it is read THEN it excludes repo-approved temporary workspace tmp/**', () => {
    const script = readTestScript()

    expect(script).toContain("--exclude 'tmp/**'")
  })

  it('GIVEN the package.json test script WHEN it is read THEN it still excludes .claude/worktrees/** (existing exclusion preserved)', () => {
    const script = readTestScript()

    expect(script).toContain("--exclude '.claude/worktrees/**'")
  })

  it('GIVEN the package.json test script WHEN it is read THEN it still excludes tests/e2e/** (existing exclusion preserved)', () => {
    const script = readTestScript()

    expect(script).toContain("--exclude 'tests/e2e/**'")
  })

  it('GIVEN the package.json test script WHEN it is read THEN it starts with a vitest run invocation', () => {
    const script = readTestScript()

    expect(script.startsWith('vitest run')).toBe(true)
  })
})
