import { describe, expect, it } from 'vitest'

import { postAgentRunReport } from '../../scripts/agent-logs/post-agent-run-report.mjs'

function createDraft() {
  return {
    schema: 'agent_run_draft/v1',
    run_id: 'run-937-001',
    target: {
      kind: 'issue',
      id: 937,
    },
    phase: 'implementation',
    actor: {
      type: 'ai_agent',
      name: 'Codex worker',
    },
    started_at: '2026-06-17T12:00:00.000Z',
  }
}

function createReport() {
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
    docs_read_refs: [],
  }
}

describe('github comment dry-run', () => {
  it('GIVEN dry-run mode WHEN posting is requested THEN no write APIs run and the summary omits the body', async () => {
    const client = {
      listIssueComments: async () => [],
      createIssueComment: async () => {
        throw new Error('create should not run in dry-run')
      },
      updateIssueComment: async () => {
        throw new Error('update should not run in dry-run')
      },
    }

    const result = await postAgentRunReport({
      draft: createDraft(),
      report: createReport(),
      repo: 'squne121/loop-protocol',
      dryRun: true,
      client,
    })

    expect(result).toMatchObject({
      action: 'create',
      repo: 'squne121/loop-protocol',
      issue_number: 937,
      pr_number: null,
      run_id: 'run-937-001',
    })
    expect('body' in result).toBe(false)
  })

  it('GIVEN live mode without explicit confirmation WHEN posting is requested THEN it fails closed before any API writes', async () => {
    const client = {
      listIssueComments: async () => {
        throw new Error('list should not run without live confirmation')
      },
      createIssueComment: async () => {
        throw new Error('create should not run without live confirmation')
      },
      updateIssueComment: async () => {
        throw new Error('update should not run without live confirmation')
      },
    }

    await expect(postAgentRunReport({
      draft: createDraft(),
      report: createReport(),
      repo: 'squne121/loop-protocol',
      dryRun: false,
      client,
    })).rejects.toThrow(/confirm-live true/)
  })

  it('GIVEN a repo outside the allowlist WHEN posting is requested THEN it fails closed before comment scans', async () => {
    const client = {
      listIssueComments: async () => {
        throw new Error('list should not run for a repo mismatch')
      },
      createIssueComment: async () => {
        throw new Error('create should not run for a repo mismatch')
      },
      updateIssueComment: async () => {
        throw new Error('update should not run for a repo mismatch')
      },
    }

    await expect(postAgentRunReport({
      draft: createDraft(),
      report: createReport(),
      repo: 'other/repo',
      client,
    })).rejects.toThrow(/allowlisted repository/)
  })
})
