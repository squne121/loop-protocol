import { describe, expect, it, vi } from 'vitest'
import { mkdtempSync, writeFileSync, rmSync } from 'fs'
import { join } from 'path'
import { tmpdir } from 'os'

import { upsertGithubMarkerCommentFromFile } from '../../scripts/agent-logs/upsert-github-marker-comment.mjs'
import { buildAgentRunReportCommentBody, validateFinalCommentBody } from '../../scripts/agent-logs/lib/github-comments.mjs'
import { renderValidatedPublicMarkdown, validateFinalReport } from '../../scripts/agent-logs/lib/validate-final-report.mjs'
import { postAgentRunReport } from '../../scripts/agent-logs/post-agent-run-report.mjs'

function createObservationSource() {
  return {
    schema_version: 'observation_source_result/v1',
    source_kind: 'claude_code',
    capability_verdict: 'supported',
    availability: 'available',
    projection_mode: 'allowlist_projection',
    safety: {
      verdict: 'pass',
      raw_values_emitted: false,
      forbidden_field_scan: 'pass',
      reason_codes: [],
    },
    metrics: {
      trace_count: 1,
      span_count: 2,
      prompt_tokens: 10,
      completion_tokens: 20,
      total_tokens: 30,
    },
    provenance: {
      schema_version: 'observation_source_provenance/v1',
      ref: {
        kind: 'observation_projection_digest',
        artifact_id: null,
        artifact_digest: null,
        workflow_run_url: null,
        schema_ref: null,
        ref: null,
        digest: 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        validation_verdict: 'pass',
      },
      source_projection_digest: 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
      validator_id: 'agent-run-report-schema',
      validator_policy_digest: 'sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
      evidence_mode: 'synthetic_only',
      checked_at: '2026-06-29T12:00:00Z',
    },
  }
}

function createReport(overrides = {}) {
  return {
    schema: 'agent_run_report/v1',
    public_surface_kind: 'github_issue_comment',
    public_safety: {
      redaction_status: 'clean',
      checked_by: 'pnpm agent-run-report:check',
      validator_version: '1.0.0',
      checked_at: '2026-06-17T12:30:00.000Z',
      verdict: 'pass',
      blocked_reasons: [],
      observation_sources: [createObservationSource()],
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
    actor: {
      type: 'ai_agent',
      name: 'Codex worker',
    },
    authority: {
      level: 'non_authoritative',
      basis: 'ai_self_report',
      evidence_refs: [],
    },
    token_usage: {
      availability: 'unavailable',
      source: 'none',
      prompt: null,
      completion: null,
      total: null,
    },
    manifest_refs: [],
    evidence_refs: [],
    commands_summary: [
      {
        command_label: 'pnpm test -- tests/agent-logs',
        exit_code: 0,
        verdict: 'pass',
        summary: 'focused tests passed',
        artifact_ref: 'artifact:agent-logs-tests',
      },
    ],
    docs_read_refs: [
      {
        ref_kind: 'issue',
        ref: 'https://github.com/squne121/loop-protocol/issues/937',
        summary: 'implementation contract reviewed',
      },
    ],
    ...overrides,
  }
}

describe('github comment post guard', () => {
  it('GIVEN a valid public report WHEN rendered for posting THEN validator round-trip and final body guard both pass', () => {
    const report = createReport()
    validateFinalReport(report)
    const payloadMarkdown = renderValidatedPublicMarkdown(report)
    const candidate = buildAgentRunReportCommentBody({
      ownership: {
        repo: 'squne121/loop-protocol',
        issueNumber: 937,
        prNumber: null,
        runId: 'run-937-001',
      },
      payloadMarkdown,
    })
    const validation = validateFinalCommentBody(candidate.body, {
      expectedOwnership: {
        repo: 'squne121/loop-protocol',
        issueNumber: 937,
        prNumber: null,
        runId: 'run-937-001',
      },
      expectedDigest: candidate.digest,
    })

    expect(validation.valid).toBe(true)
  })

  it('GIVEN a report that fails the validator WHEN rendered for posting THEN post guard rejects before any comment body exists', () => {
    const report = createReport({
      public_safety: {
        redaction_status: 'dirty',
        checked_by: 'pnpm agent-run-report:check',
        validator_version: '1.0.0',
        checked_at: '2026-06-17T12:30:00.000Z',
        verdict: 'pass',
        blocked_reasons: [],
        observation_sources: [createObservationSource()],
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
    })

    expect(() => renderValidatedPublicMarkdown(report)).toThrow(/public_surface_redaction_status/)
  })

  it('GIVEN a report with missing entirecli_safety WHEN validateFinalReport is called THEN it fails closed before any GitHub comment call', async () => {
    const report = createReport({
      public_safety: {
        redaction_status: 'clean',
        checked_by: 'pnpm agent-run-report:check',
        validator_version: '1.0.0',
        checked_at: '2026-06-17T12:30:00.000Z',
        verdict: 'pass',
        blocked_reasons: [],
        observation_sources: [createObservationSource()],
        // no entirecli_safety
      },
    })

    const client = {
      listIssueComments: async () => { throw new Error('list must not run for missing entirecli_safety') },
      createIssueComment: async () => { throw new Error('create must not run for missing entirecli_safety') },
      updateIssueComment: async () => { throw new Error('update must not run for missing entirecli_safety') },
    }

    expect(() => validateFinalReport(report)).toThrow(/entirecli_safety/)
    await expect(
      upsertGithubMarkerCommentFromFile({
        repo: 'squne121/loop-protocol',
        targetNumber: 937,
        issueNumber: 937,
        prNumber: null,
        runId: 'run-937-001',
        payloadMarkdownFile: '/dev/null',
        dryRun: true,
        client,
      })
    ).rejects.not.toThrow('list must not run')
  })

  it('GIVEN a report with entirecli_safety verdict blocked WHEN validateFinalReport is called THEN it fails closed before any GitHub comment call', () => {
    const report = createReport({
      public_safety: {
        redaction_status: 'clean',
        checked_by: 'pnpm agent-run-report:check',
        validator_version: '1.0.0',
        checked_at: '2026-06-17T12:30:00.000Z',
        verdict: 'pass',
        blocked_reasons: [],
        observation_sources: [createObservationSource()],
        entirecli_safety: {
          schema_version: 'entirecli_safety_result/v1',
          verdict: 'blocked',
          reason_codes: ['push_sessions_enabled'],
          raw_values_emitted: false,
          checked_surfaces: {
            entire_binary: true,
            entire_version: 'v1.2***[len=12]',
            entire_enable_help: true,
            entire_configure_help: true,
          },
        },
      },
    })

    expect(() => validateFinalReport(report)).toThrow(/entirecli_safety/)
  })

  it('GIVEN the helper CLI surface WHEN live posting is requested THEN it fails closed and keeps live writes on the validated-report path only', async () => {
    const tempDir = mkdtempSync(join(tmpdir(), 'agent-run-post-guard-'))
    const payloadPath = join(tempDir, 'payload.md')
    try {
      writeFileSync(payloadPath, renderValidatedPublicMarkdown(createReport()), 'utf-8')
      const client = {
        listIssueComments: async () => {
          throw new Error('list should not run for helper live posting')
        },
        createIssueComment: async () => {
          throw new Error('create should not run for helper live posting')
        },
        updateIssueComment: async () => {
          throw new Error('update should not run for helper live posting')
        },
      }

      await expect(upsertGithubMarkerCommentFromFile({
        repo: 'squne121/loop-protocol',
        targetNumber: 937,
        issueNumber: 937,
        prNumber: null,
        runId: 'run-937-001',
        payloadMarkdownFile: payloadPath,
        dryRun: false,
        client,
      })).rejects.toThrow(/live posting is disabled/)
    } finally {
      rmSync(tempDir, { recursive: true, force: true })
    }
  })

  it('GIVEN non-canonical payload markdown WHEN the helper surface is used in dry-run THEN validation fails before any comment scan', async () => {
    const tempDir = mkdtempSync(join(tmpdir(), 'agent-run-post-guard-'))
    const payloadPath = join(tempDir, 'payload.md')
    try {
      writeFileSync(payloadPath, '```md\n/home/squne/leak\n```', 'utf-8')
      const client = {
        listIssueComments: async () => {
          throw new Error('list should not run for invalid payload markdown')
        },
        createIssueComment: async () => {
          throw new Error('create should not run for invalid payload markdown')
        },
        updateIssueComment: async () => {
          throw new Error('update should not run for invalid payload markdown')
        },
      }

      await expect(upsertGithubMarkerCommentFromFile({
        repo: 'squne121/loop-protocol',
        targetNumber: 937,
        issueNumber: 937,
        prNumber: null,
        runId: 'run-937-001',
        payloadMarkdownFile: payloadPath,
        dryRun: true,
        client,
      })).rejects.toThrow(/duplicate_start_marker/)
    } finally {
      rmSync(tempDir, { recursive: true, force: true })
    }
  })

  it('GIVEN a report with missing entirecli_safety WHEN postAgentRunReport is called with spy client THEN it rejects and GitHub client is never invoked', async () => {
    const report = createReport({
      public_safety: {
        redaction_status: 'clean',
        checked_by: 'pnpm agent-run-report:check',
        validator_version: '1.0.0',
        checked_at: '2026-06-17T12:30:00.000Z',
        verdict: 'pass',
        blocked_reasons: [],
        observation_sources: [createObservationSource()],
        // entirecli_safety absent — must fail-closed
      },
    })
    const mockClient = {
      listIssueComments: vi.fn(),
      createIssueComment: vi.fn(),
      updateIssueComment: vi.fn(),
    }
    const draft = {
      schema: 'agent_run_draft/v1',
      run_id: 'run-937-001',
      target: { kind: 'issue', id: 937 },
      phase: 'implementation',
      actor: { type: 'ai_agent', name: 'test' },
      started_at: '2026-06-17T12:30:00.000Z',
    }

    await expect(
      postAgentRunReport({ draft, report, repo: 'squne121/loop-protocol', dryRun: true, confirmLive: false, client: mockClient })
    ).rejects.toThrow(/entirecli_safety/)

    // validateFinalReport threw before upsertAgentRunReportComment could invoke any client method
    expect(mockClient.listIssueComments).not.toHaveBeenCalled()
    expect(mockClient.createIssueComment).not.toHaveBeenCalled()
    expect(mockClient.updateIssueComment).not.toHaveBeenCalled()
  })

  it('GIVEN a report with entirecli_safety verdict blocked WHEN postAgentRunReport is called with spy client THEN it rejects and GitHub client is never invoked', async () => {
    const report = createReport({
      public_safety: {
        redaction_status: 'clean',
        checked_by: 'pnpm agent-run-report:check',
        validator_version: '1.0.0',
        checked_at: '2026-06-17T12:30:00.000Z',
        verdict: 'pass',
        blocked_reasons: [],
        observation_sources: [createObservationSource()],
        entirecli_safety: {
          schema_version: 'entirecli_safety_result/v1',
          verdict: 'blocked',
          reason_codes: ['push_sessions_enabled'],
          raw_values_emitted: false,
          checked_surfaces: {
            entire_binary: true,
            entire_version: 'v1.2***[len=12]',
            entire_enable_help: true,
            entire_configure_help: true,
          },
        },
      },
    })
    const mockClient = {
      listIssueComments: vi.fn(),
      createIssueComment: vi.fn(),
      updateIssueComment: vi.fn(),
    }
    const draft = {
      schema: 'agent_run_draft/v1',
      run_id: 'run-937-001',
      target: { kind: 'issue', id: 937 },
      phase: 'implementation',
      actor: { type: 'ai_agent', name: 'test' },
      started_at: '2026-06-17T12:30:00.000Z',
    }

    await expect(
      postAgentRunReport({ draft, report, repo: 'squne121/loop-protocol', dryRun: true, confirmLive: false, client: mockClient })
    ).rejects.toThrow(/entirecli_safety/)

    expect(mockClient.listIssueComments).not.toHaveBeenCalled()
    expect(mockClient.createIssueComment).not.toHaveBeenCalled()
    expect(mockClient.updateIssueComment).not.toHaveBeenCalled()
  })

  it('GIVEN a report with raw_values_emitted true WHEN postAgentRunReport is called with spy client THEN it rejects and GitHub client is never invoked', async () => {
    const report = createReport({
      public_safety: {
        redaction_status: 'clean',
        checked_by: 'pnpm agent-run-report:check',
        validator_version: '1.0.0',
        checked_at: '2026-06-17T12:30:00.000Z',
        verdict: 'pass',
        blocked_reasons: [],
        observation_sources: [createObservationSource()],
        entirecli_safety: {
          schema_version: 'entirecli_safety_result/v1',
          verdict: 'not_applicable',
          reason_codes: ['entire_absent'],
          raw_values_emitted: true,
          checked_surfaces: {
            entire_binary: false,
            entire_version: null,
            entire_enable_help: false,
            entire_configure_help: false,
          },
        },
      },
    })
    const mockClient = {
      listIssueComments: vi.fn(),
      createIssueComment: vi.fn(),
      updateIssueComment: vi.fn(),
    }
    const draft = {
      schema: 'agent_run_draft/v1',
      run_id: 'run-937-001',
      target: { kind: 'issue', id: 937 },
      phase: 'implementation',
      actor: { type: 'ai_agent', name: 'test' },
      started_at: '2026-06-17T12:30:00.000Z',
    }

    await expect(
      postAgentRunReport({ draft, report, repo: 'squne121/loop-protocol', dryRun: true, confirmLive: false, client: mockClient })
    ).rejects.toThrow(/entirecli_safety/)

    expect(mockClient.listIssueComments).not.toHaveBeenCalled()
    expect(mockClient.createIssueComment).not.toHaveBeenCalled()
    expect(mockClient.updateIssueComment).not.toHaveBeenCalled()
  })
})
