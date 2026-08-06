/**
 * eslint-ignore-policy.test.ts
 *
 * Issue #1995: repo-approved local temporary workspace を tmp/ へ一本化し
 * .claude/tmp/ を非推奨化する。
 *
 * eslint.config.mjs の ignores 配列に '.claude/tmp/**' と 'tmp/**' が
 * 追加されたことを、ESLint Node.js API（`ESLint#isPathIgnored()` /
 * `ESLint#lintText()`）により in-process で検証する。
 *
 * OWNER 敵対的レビュー（PR #2001 issuecomment-5203294136）を受け、実ファイル
 * fixture の固定パス書き込みや `pnpm lint` の子プロセス多重起動は行わない。
 *
 * Covers AC3, AC4, AC5 from Issue #1995 (Runtime Verification Applicability:
 * decision: immediate).
 */

import { ESLint } from 'eslint'
import { resolve } from 'path'
import { describe, expect, it } from 'vitest'

const REPO_ROOT = resolve(__dirname, '..')

// Invalid JS content guaranteed to produce an ESLint parse error if linted.
const INVALID_JS_CONTENT = 'const x = {\n'

function createEslint(): ESLint {
  return new ESLint({
    cwd: REPO_ROOT,
    overrideConfigFile: resolve(REPO_ROOT, 'eslint.config.mjs'),
  })
}

describe('eslint-ignore-policy (Issue #1995)', () => {
  it('GIVEN .claude/tmp/__fixture__/invalid.js THEN ignored=true', async () => {
    const eslint = createEslint()
    const targetPath = resolve(REPO_ROOT, '.claude/tmp/__fixture__/invalid.js')

    const ignored = await eslint.isPathIgnored(targetPath)

    expect(ignored).toBe(true)
  })

  it('GIVEN nested/.claude/tmp/__fixture__/invalid.js THEN ignored=true', async () => {
    const eslint = createEslint()
    const targetPath = resolve(REPO_ROOT, 'nested/.claude/tmp/__fixture__/invalid.js')

    const ignored = await eslint.isPathIgnored(targetPath)

    expect(ignored).toBe(true)
  })

  it('GIVEN tmp/__fixture__/invalid.js THEN ignored=true', async () => {
    const eslint = createEslint()
    const targetPath = resolve(REPO_ROOT, 'tmp/__fixture__/invalid.js')

    const ignored = await eslint.isPathIgnored(targetPath)

    expect(ignored).toBe(true)
  })

  it('GIVEN src/__fixture__.ts THEN ignored=false', async () => {
    const eslint = createEslint()
    const targetPath = resolve(REPO_ROOT, 'src/__fixture__.ts')

    const ignored = await eslint.isPathIgnored(targetPath)

    expect(ignored).toBe(false)
  })

  it('GIVEN .claude/tmp/__fixture__/invalid.js WHEN lintText runs THEN the ignored path is not linted at all', async () => {
    const eslint = createEslint()
    const filePath = resolve(REPO_ROOT, '.claude/tmp/__fixture__/invalid.js')

    // warnIgnored: false — do not synthesize an "ignored file" warning result;
    // an ignored path must simply produce no lint result (not scanned).
    const results = await eslint.lintText(INVALID_JS_CONTENT, { filePath, warnIgnored: false })

    expect(results).toHaveLength(0)
  })

  it('GIVEN tmp/__fixture__/invalid.js WHEN lintText runs THEN the ignored path is not linted at all', async () => {
    const eslint = createEslint()
    const filePath = resolve(REPO_ROOT, 'tmp/__fixture__/invalid.js')

    const results = await eslint.lintText(INVALID_JS_CONTENT, { filePath, warnIgnored: false })

    expect(results).toHaveLength(0)
  })

  it('GIVEN src/__fixture__.ts WHEN lintText runs THEN lint errors are actually reported (negative control)', async () => {
    const eslint = createEslint()
    const filePath = resolve(REPO_ROOT, 'src/__fixture__.ts')

    const results = await eslint.lintText(INVALID_JS_CONTENT, { filePath })

    expect(results).toHaveLength(1)
    expect(results[0].errorCount).toBeGreaterThan(0)
  })
})
