import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const root = resolve(import.meta.dirname, '..', '..')
const source = readFileSync(resolve(root, 'scripts/session-recording/codex-hook-adapter.mjs'), 'utf8')

describe('Codex passive recorder boundary', () => {
  it('contains no network, child-process, git, GitHub, or decision implementation', () => {
    expect(source).not.toMatch(/node:(?:http|https|net|tls|child_process)/)
    expect(source).not.toMatch(/\b(?:fetch|execFile|spawn|git|github|permissionDecision|additionalContext)\b/i)
  })
})
