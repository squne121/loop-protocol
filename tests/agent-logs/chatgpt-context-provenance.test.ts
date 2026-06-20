import { describe, expect, it } from 'vitest'
import { writeFileSync } from 'fs'
import { resolve } from 'path'
import { mkdtempSync, rmSync } from 'fs'
import { tmpdir } from 'os'

import { loadSources, computeDigest } from '../../scripts/agent-logs/lib/chatgpt-context-source-loader.mjs'
import { readFileSync } from 'fs'

function createTempDir() {
  return mkdtempSync(resolve(tmpdir(), 'chatgpt-provenance-'))
}

function cleanupTempDir(dir: string) {
  rmSync(dir, { recursive: true, force: true })
}

function makeMinimalIssue(number: number) {
  return { number, title: `Issue #${number}` }
}

describe('chatgpt-context provenance / source_manifest (AC8)', () => {
  it('GIVEN valid source files WHEN loading THEN manifest is returned', async () => {
    const tempDir = createTempDir()
    try {
      const parentIssueJson = resolve(tempDir, 'parent.json')
      const targetIssueJson = resolve(tempDir, 'target.json')
      const retroIndexJson = resolve(tempDir, 'retro.json')
      const sourceSetJson = resolve(tempDir, 'source-set.json')

      writeFileSync(parentIssueJson, JSON.stringify(makeMinimalIssue(928)))
      writeFileSync(targetIssueJson, JSON.stringify(makeMinimalIssue(939)))
      writeFileSync(retroIndexJson, JSON.stringify({ schema: 'agent_retro_index/v1', entries: [] }))
      writeFileSync(sourceSetJson, JSON.stringify({ schema: 'source_set/v1', sources: [] }))

      const { manifest } = await loadSources({
        parentIssueJson,
        targetIssueJson,
        retroIndexJson,
        sourceSetJson,
        runReportJson: [],
        evidenceRefJson: [],
      })

      expect(Array.isArray(manifest)).toBe(true)
      expect(manifest.length).toBeGreaterThanOrEqual(4)
    } finally {
      cleanupTempDir(tempDir)
    }
  })

  it('GIVEN loaded sources WHEN inspecting manifest entries THEN each has source_kind', async () => {
    const tempDir = createTempDir()
    try {
      const parentIssueJson = resolve(tempDir, 'parent.json')
      const targetIssueJson = resolve(tempDir, 'target.json')
      const retroIndexJson = resolve(tempDir, 'retro.json')
      const sourceSetJson = resolve(tempDir, 'source-set.json')

      writeFileSync(parentIssueJson, JSON.stringify(makeMinimalIssue(928)))
      writeFileSync(targetIssueJson, JSON.stringify(makeMinimalIssue(939)))
      writeFileSync(retroIndexJson, JSON.stringify({ entries: [] }))
      writeFileSync(sourceSetJson, JSON.stringify({ sources: [] }))

      const { manifest } = await loadSources({
        parentIssueJson,
        targetIssueJson,
        retroIndexJson,
        sourceSetJson,
        runReportJson: [],
        evidenceRefJson: [],
      })

      for (const entry of manifest) {
        expect(typeof entry.source_kind).toBe('string')
        expect(entry.source_kind.length).toBeGreaterThan(0)
      }
    } finally {
      cleanupTempDir(tempDir)
    }
  })

  it('GIVEN loaded sources WHEN inspecting manifest entries THEN each has source_ref', async () => {
    const tempDir = createTempDir()
    try {
      const parentIssueJson = resolve(tempDir, 'parent.json')
      const targetIssueJson = resolve(tempDir, 'target.json')
      const retroIndexJson = resolve(tempDir, 'retro.json')
      const sourceSetJson = resolve(tempDir, 'source-set.json')

      writeFileSync(parentIssueJson, JSON.stringify(makeMinimalIssue(928)))
      writeFileSync(targetIssueJson, JSON.stringify(makeMinimalIssue(939)))
      writeFileSync(retroIndexJson, JSON.stringify({}))
      writeFileSync(sourceSetJson, JSON.stringify({}))

      const { manifest } = await loadSources({
        parentIssueJson,
        targetIssueJson,
        retroIndexJson,
        sourceSetJson,
        runReportJson: [],
        evidenceRefJson: [],
      })

      for (const entry of manifest) {
        expect(typeof entry.source_ref).toBe('string')
      }
    } finally {
      cleanupTempDir(tempDir)
    }
  })

  it('GIVEN loaded sources WHEN inspecting manifest entries THEN each has canonical_digest and body_digest', async () => {
    const tempDir = createTempDir()
    try {
      const parentIssueJson = resolve(tempDir, 'parent.json')
      const targetIssueJson = resolve(tempDir, 'target.json')
      const retroIndexJson = resolve(tempDir, 'retro.json')
      const sourceSetJson = resolve(tempDir, 'source-set.json')

      writeFileSync(parentIssueJson, JSON.stringify(makeMinimalIssue(928)))
      writeFileSync(targetIssueJson, JSON.stringify(makeMinimalIssue(939)))
      writeFileSync(retroIndexJson, JSON.stringify({}))
      writeFileSync(sourceSetJson, JSON.stringify({}))

      const { manifest } = await loadSources({
        parentIssueJson,
        targetIssueJson,
        retroIndexJson,
        sourceSetJson,
        runReportJson: [],
        evidenceRefJson: [],
      })

      for (const entry of manifest) {
        expect(typeof entry.canonical_digest).toBe('string')
        expect(entry.canonical_digest).toMatch(/^sha256:/)
        expect(typeof entry.body_digest).toBe('string')
        expect(entry.body_digest).toMatch(/^sha256:/)
      }
    } finally {
      cleanupTempDir(tempDir)
    }
  })

  it('GIVEN a file on disk WHEN computing digest THEN digest matches computeDigest(file content)', async () => {
    const tempDir = createTempDir()
    try {
      const filePath = resolve(tempDir, 'test.json')
      const content = JSON.stringify({ foo: 'bar', baz: 42 })
      writeFileSync(filePath, content)

      const expected = computeDigest(Buffer.from(content))
      expect(expected).toMatch(/^sha256:[a-f0-9]{64}$/)

      // Load via loadSources single file
      const parentIssueJson = resolve(tempDir, 'parent.json')
      writeFileSync(parentIssueJson, content)

      const minimalJson = resolve(tempDir, 'minimal.json')
      writeFileSync(minimalJson, JSON.stringify({}))

      const { manifest } = await loadSources({
        parentIssueJson,
        targetIssueJson: minimalJson,
        retroIndexJson: minimalJson,
        sourceSetJson: minimalJson,
        runReportJson: [],
        evidenceRefJson: [],
      })

      const parentEntry = manifest.find((e: { source_kind: string }) => e.source_kind === 'parent_issue_json')
      expect(parentEntry?.body_digest).toBe(expected)
    } finally {
      cleanupTempDir(tempDir)
    }
  })
})
