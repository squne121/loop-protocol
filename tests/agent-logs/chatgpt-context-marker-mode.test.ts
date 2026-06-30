import { describe, expect, it } from 'vitest'
import { execFileSync } from 'child_process'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { resolve } from 'path'

import { renderPublicMarkdown } from '../../scripts/lib/agent-run-report-validation.mjs'
import {
  buildChatgptRetroContextCommentBody,
  computeChatgptRetroContextPayloadDigest,
} from '../../scripts/agent-logs/lib/chatgpt-retro-context-marker-helper.mjs'
import { buildRetroIndexCommentBody } from '../../scripts/agent-logs/lib/retro-index-comment-helper.mjs'
import { buildAgentRunReportCommentBody } from '../../scripts/agent-logs/lib/github-comments.mjs'
import { buildSourceCommentSetDigest } from '../../scripts/agent-logs/lib/retro-index-builder.mjs'
import { createValidObservationSourceResult } from './report-test-fixtures'

const REPO_ROOT = resolve(__dirname, '..', '..')
const EXPORT_SCRIPT = resolve(REPO_ROOT, 'scripts', 'agent-logs', 'export-chatgpt-context.mjs')

function makeTempDir() {
  return mkdtempSync(resolve(tmpdir(), 'chatgpt-marker-mode-'))
}

function cleanup(dir: string) {
  rmSync(dir, { recursive: true, force: true })
}

describe('chatgpt context marker-origin mode', () => {
  it('GIVEN a valid context marker and referenced comment fixtures WHEN export runs THEN it succeeds from marker mode', () => {
    const tempDir = makeTempDir()
    try {
      const reportPayload = {
        schema: 'agent_run_report/v1',
        public_surface_kind: 'github_issue_comment',
        public_safety: {
          redaction_status: 'clean',
          checked_by: 'pnpm agent-run-report:check',
          validator_version: '1.0.0',
          checked_at: '2026-07-01T00:00:00.000Z',
          verdict: 'pass',
          blocked_reasons: [],
          observation_sources: [createValidObservationSourceResult()],
          entirecli_safety: {
            schema_version: 'entirecli_safety_result/v1',
            verdict: 'not_applicable',
            reason_codes: ['entire_absent'],
            raw_values_emitted: false,
            checked_surfaces: {
              entire_binary: false,
              entire_version: null,
              entire_enable_help: false,
              entire_configure_help: false,
            },
          },
        },
        actor: { type: 'ai_agent', name: 'Codex worker' },
        authority: { level: 'non_authoritative', basis: 'ai_self_report', evidence_refs: [] },
        token_usage: { availability: 'unavailable', source: 'none', prompt: null, completion: null, total: null },
        manifest_refs: [],
        evidence_refs: [],
        commands_summary: [
          {
            command_label: 'pnpm test -- tests/agent-logs',
            exit_code: 0,
            verdict: 'pass',
            summary: 'passed',
            artifact_ref: 'artifact:agent-logs-tests',
          },
        ],
        docs_read_refs: [],
      }
      const reportMarkdown = renderPublicMarkdown(reportPayload)
      const reportComment = buildAgentRunReportCommentBody({
        ownership: {
          repo: 'squne121/loop-protocol',
          issueNumber: 1224,
          prNumber: null,
          runId: 'run-1224-001',
        },
        payloadMarkdown: reportMarkdown,
      })
      const reportDigest = `sha256:${reportComment.digest}`

      const retroPayload = {
        schema: 'agent_retro_index/v1',
        generation_verdict: 'complete',
        entries: [
          {
            report_comment_url: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-11',
            report_digest: reportDigest,
            issue: 1224,
            pr: 1300,
            merge_sha: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            tags: ['retro'],
            friction_summary: 'safe',
            quality_signals: ['deterministic'],
            follow_up_issues: [],
          },
        ],
        orphan_reports: [],
        ambiguous_links: [],
      }
      const retroMarkdown = renderPublicMarkdown(retroPayload)
      const retroSourceSetDigest = buildSourceCommentSetDigest([
        {
          comment_url: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-11',
          source_kind: 'issues',
          source_number: 1224,
          body_digest: reportDigest,
        },
        {
          comment_url: 'https://github.com/squne121/loop-protocol/issues/1153#issuecomment-12',
          source_kind: 'issues',
          source_number: 1153,
          body_digest: 'sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
        },
      ])
      const retroComment = buildRetroIndexCommentBody({
        repo: 'squne121/loop-protocol',
        parentIssue: 1153,
        algorithm: 'retro-index-builder@1',
        payloadMarkdown: retroMarkdown,
        canonicalIndexDigest: 'sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
        sourceCommentSetDigest: retroSourceSetDigest,
      })

      const markerPayload = {
        schema: 'chatgpt_retro_context_marker/v1',
        marker_kind: 'CHATGPT_RETRO_CONTEXT_V1',
        repo: 'squne121/loop-protocol',
        target: { type: 'issue', number: 1224 },
        parent_issue: 1153,
        canonicalization: {
          algorithm: 'canonical-json-v1',
          payload_digest: 'sha256:0000000000000000000000000000000000000000000000000000000000000000',
        },
        refs: {
          run_reports: [
            {
              comment_url: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-11',
              payload_digest: reportDigest,
              schema_ref: 'docs/schemas/agent-run-report.schema.json#agent_run_report/v1',
              validation_verdict: 'pass',
              supersedes_digest: null,
            },
          ],
          retro_index: {
            comment_url: 'https://github.com/squne121/loop-protocol/issues/1153#issuecomment-12',
            payload_digest: 'sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
            source_set_digest: retroSourceSetDigest,
            schema_ref: 'docs/schemas/agent-retro-index.schema.json#agent_retro_index/v1',
            validation_verdict: 'pass',
          },
        },
        safety: {
          untrusted_evidence_mode: 'typed_refs_only',
          free_form_instructions_present: false,
          forbidden_fields_scan: 'pass',
          rendered_markdown_scan: 'pass',
          raw_values_emitted: false,
        },
        prerequisites: {
          containment_issue: 1157,
          pilot_exception_issue: 1220,
          capability_matrix_issue: 1221,
          schema_issue: 1222,
          adapter_issue: 1223,
          real_pilot_allowed: false,
          evidence_mode: 'synthetic_only',
        },
        created_at: '2026-07-01T00:00:00.000Z',
      }
      const initialMarkerMarkdown = renderPublicMarkdown(markerPayload)
      expect(initialMarkerMarkdown).toContain('CHATGPT_RETRO_CONTEXT_V1')
      markerPayload.canonicalization.payload_digest = computeChatgptRetroContextPayloadDigest(markerPayload)
      const markerMarkdown = renderPublicMarkdown(markerPayload)
      const markerComment = buildChatgptRetroContextCommentBody({
        ownership: {
          repo: 'squne121/loop-protocol',
          targetType: 'issue',
          targetNumber: 1224,
          parentIssue: 1153,
        },
        payloadMarkdown: markerMarkdown,
      })

      const markerFile = resolve(tempDir, 'marker.json')
      const commentsFile = resolve(tempDir, 'comments.json')
      const outputFile = resolve(tempDir, 'bundle.md')
      const summaryFile = resolve(tempDir, 'summary.json')

      writeFileSync(markerFile, JSON.stringify({
        id: 21,
        html_url: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-21',
        body: markerComment.body,
      }))
      writeFileSync(commentsFile, JSON.stringify([
        {
          id: 11,
          html_url: 'https://github.com/squne121/loop-protocol/issues/1224#issuecomment-11',
          body: reportComment.body,
        },
        {
          id: 12,
          html_url: 'https://github.com/squne121/loop-protocol/issues/1153#issuecomment-12',
          body: retroComment.body,
        },
      ]))

      const stdout = execFileSync(process.execPath, [
        EXPORT_SCRIPT,
        '--marker-comment-json', markerFile,
        '--github-comments-json', commentsFile,
        '--max-chars', '100000',
        '--max-sections', '20',
        '--generated-at', '2026-07-01T00:00:00.000Z',
        '--output', outputFile,
        '--summary-json-out', summaryFile,
      ], {
        cwd: REPO_ROOT,
        encoding: 'utf-8',
      })

      expect(stdout).toContain('chatgpt-context: bundle written')
      const content = readFileSync(outputFile, 'utf-8')
      expect(content).toContain('SECURITY_BOUNDARY')
      expect(content).toContain('run_report_comment[0]')
      expect(content).not.toContain('/home/')
    } finally {
      cleanup(tempDir)
    }
  })

  it('GIVEN marker mode and legacy JSON mode together WHEN export runs THEN it rejects mixed source modes', () => {
    const tempDir = makeTempDir()
    try {
      const markerFile = resolve(tempDir, 'marker.json')
      const commentsFile = resolve(tempDir, 'comments.json')
      const outputFile = resolve(tempDir, 'bundle.md')
      const summaryFile = resolve(tempDir, 'summary.json')
      const legacyFile = resolve(tempDir, 'legacy.json')
      writeFileSync(markerFile, JSON.stringify({ body: 'x' }))
      writeFileSync(commentsFile, JSON.stringify([]))
      writeFileSync(legacyFile, JSON.stringify({}))

      expect(() => execFileSync(process.execPath, [
        EXPORT_SCRIPT,
        '--marker-comment-json', markerFile,
        '--github-comments-json', commentsFile,
        '--parent-issue-json', legacyFile,
        '--target-issue-json', legacyFile,
        '--retro-index-json', legacyFile,
        '--source-set-json', legacyFile,
        '--max-chars', '100000',
        '--max-sections', '20',
        '--generated-at', '2026-07-01T00:00:00.000Z',
        '--output', outputFile,
        '--summary-json-out', summaryFile,
      ], {
        cwd: REPO_ROOT,
        encoding: 'utf-8',
        stdio: 'pipe',
      })).toThrow(/cli\.mixed_source_mode/)
    } finally {
      cleanup(tempDir)
    }
  })
})
