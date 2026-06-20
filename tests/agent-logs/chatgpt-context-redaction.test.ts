import { describe, expect, it } from 'vitest'
import { mkdirSync, writeFileSync } from 'fs'
import { resolve } from 'path'
import { tmpdir } from 'os'
import { mkdtempSync, rmSync } from 'fs'

import { FORBIDDEN_FIELDS, scanForForbiddenFields } from '../../scripts/agent-logs/lib/chatgpt-context-source-loader.mjs'

function createTempDir() {
  return mkdtempSync(resolve(tmpdir(), 'chatgpt-redaction-'))
}

function cleanupTempDir(dir: string) {
  rmSync(dir, { recursive: true, force: true })
}

describe('chatgpt-context redaction (AC1)', () => {
  describe('FORBIDDEN_FIELDS list', () => {
    it('GIVEN the forbidden fields spec WHEN checking list THEN all AC1 fields are present', () => {
      expect(FORBIDDEN_FIELDS).toContain('raw_transcript')
      expect(FORBIDDEN_FIELDS).toContain('transcript_excerpt')
      expect(FORBIDDEN_FIELDS).toContain('full_command_output')
      expect(FORBIDDEN_FIELDS).toContain('stdout')
      expect(FORBIDDEN_FIELDS).toContain('stderr')
      expect(FORBIDDEN_FIELDS).toContain('local_path')
    })
  })

  describe('scanForForbiddenFields', () => {
    it('GIVEN clean JSON WHEN scanning THEN no violations are reported', () => {
      const violations: string[] = []
      scanForForbiddenFields({ title: 'hello', ref: 'https://example.com' }, 'root', violations)
      expect(violations).toHaveLength(0)
    })

    it('GIVEN JSON with raw_transcript WHEN scanning THEN violation is reported', () => {
      const violations: string[] = []
      scanForForbiddenFields({ raw_transcript: 'some content' }, 'source', violations)
      expect(violations).toHaveLength(1)
      expect(violations[0]).toContain('raw_transcript')
    })

    it('GIVEN JSON with transcript_excerpt WHEN scanning THEN violation is reported', () => {
      const violations: string[] = []
      scanForForbiddenFields({ transcript_excerpt: 'partial...' }, 'source', violations)
      expect(violations).toHaveLength(1)
      expect(violations[0]).toContain('transcript_excerpt')
    })

    it('GIVEN JSON with full_command_output WHEN scanning THEN violation is reported', () => {
      const violations: string[] = []
      scanForForbiddenFields({ full_command_output: 'ls -la output' }, 'source', violations)
      expect(violations).toHaveLength(1)
      expect(violations[0]).toContain('full_command_output')
    })

    it('GIVEN JSON with stdout field WHEN scanning THEN violation is reported', () => {
      const violations: string[] = []
      scanForForbiddenFields({ stdout: '...output...' }, 'source', violations)
      expect(violations).toHaveLength(1)
      expect(violations[0]).toContain('stdout')
    })

    it('GIVEN JSON with stderr field WHEN scanning THEN violation is reported', () => {
      const violations: string[] = []
      scanForForbiddenFields({ stderr: 'error output' }, 'source', violations)
      expect(violations).toHaveLength(1)
      expect(violations[0]).toContain('stderr')
    })

    it('GIVEN JSON with local_path WHEN scanning THEN violation is reported', () => {
      const violations: string[] = []
      scanForForbiddenFields({ local_path: '/home/user/secret' }, 'source', violations)
      expect(violations).toHaveLength(1)
      expect(violations[0]).toContain('local_path')
    })

    it('GIVEN deeply nested forbidden field WHEN scanning THEN violation is reported with path', () => {
      const violations: string[] = []
      const obj = { commands_summary: [{ label: 'test', stdout: 'output text' }] }
      scanForForbiddenFields(obj, 'root', violations)
      expect(violations).toHaveLength(1)
      expect(violations[0]).toContain('stdout')
    })

    it('GIVEN multiple forbidden fields WHEN scanning THEN all violations are reported', () => {
      const violations: string[] = []
      const obj = { raw_transcript: 'abc', local_path: '/foo', title: 'ok' }
      scanForForbiddenFields(obj, 'root', violations)
      expect(violations).toHaveLength(2)
    })
  })
})
