import { describe, expect, it } from 'vitest'

import { GithubApiError, upsertAgentRunReportComment } from '../../scripts/agent-logs/lib/github-comments.mjs'

describe('github comment upsert error handling', () => {
  it('GIVEN duplicate matching marker comments WHEN upsert runs THEN it fails closed before writes', async () => {
    const body = [
      '<!-- agent_run_report:v1 repo=squne121/loop-protocol issue=937 pr=null run_id=run-937-001 -->',
      '<!-- agent_run_report_digest:v1 sha256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa -->',
      '',
      '<!-- agent_run_report:v1 start -->',
      '````json',
      '{"schema":"agent_run_report/v1","public_surface_kind":"github_issue_comment","public_safety":{"redaction_status":"clean","checked_by":"pnpm agent-run-report:check","validator_version":"1.0.0","checked_at":"2026-06-17T12:30:00.000Z","verdict":"pass","blocked_reasons":[]},"actor":{"type":"ai_agent","name":"Codex worker"},"authority":{"level":"non_authoritative","basis":"ai_self_report","evidence_refs":[]},"token_usage":{"availability":"unavailable","source":"none","prompt":null,"completion":null,"total":null},"manifest_refs":[],"evidence_refs":[],"commands_summary":[],"docs_read_refs":[]}',
      '````',
      '<!-- agent_run_report:v1 end -->',
    ].join('\n')
    const client = {
      listIssueComments: async () => [{ id: 1, body }, { id: 2, body }],
      createIssueComment: async () => {
        throw new Error('create should not run')
      },
      updateIssueComment: async () => {
        throw new Error('update should not run')
      },
    }

    await expect(upsertAgentRunReportComment(client, {
      repo: 'squne121/loop-protocol',
      targetNumber: 937,
      issueNumber: 937,
      prNumber: null,
      runId: 'run-937-001',
      payloadMarkdown: body.split('\n').slice(3).join('\n'),
    })).rejects.toThrow(/multiple existing comments match/)
  })

  it('GIVEN permission and transport failures WHEN surfaced by the client THEN structured GitHub API errors remain classifiable', async () => {
    const createError = new GithubApiError('permission denied', {
      httpStatus: 403,
      reasonCode: 'permission_denied',
      errorBody: '{"message":"Resource not accessible by integration"}',
    })
    const client = {
      listIssueComments: async () => [],
      createIssueComment: async () => {
        throw createError
      },
      updateIssueComment: async () => {
        throw new Error('update should not run')
      },
    }

    await expect(upsertAgentRunReportComment(client, {
      repo: 'squne121/loop-protocol',
      targetNumber: 937,
      issueNumber: 937,
      prNumber: null,
      runId: 'run-937-001',
      payloadMarkdown: '<!-- agent_run_report:v1 start -->\n````json\n{"schema":"agent_run_report/v1","public_surface_kind":"github_issue_comment","public_safety":{"redaction_status":"clean","checked_by":"pnpm agent-run-report:check","validator_version":"1.0.0","checked_at":"2026-06-17T12:30:00.000Z","verdict":"pass","blocked_reasons":[]},"actor":{"type":"ai_agent","name":"Codex worker"},"authority":{"level":"non_authoritative","basis":"ai_self_report","evidence_refs":[]},"token_usage":{"availability":"unavailable","source":"none","prompt":null,"completion":null,"total":null},"manifest_refs":[],"evidence_refs":[],"commands_summary":[],"docs_read_refs":[]}\n````\n<!-- agent_run_report:v1 end -->',
    })).rejects.toMatchObject({
      httpStatus: 403,
      reasonCode: 'permission_denied',
    })
  })

  it('GIVEN create/update validation failures WHEN mocked REST throws 404/410/422 THEN those statuses remain visible to callers', async () => {
    for (const [httpStatus, reasonCode] of [
      [404, 'not_found'],
      [410, 'gone'],
      [422, 'validation_failed'],
    ]) {
      const client = {
        listIssueComments: async () => [],
        createIssueComment: async () => {
          throw new GithubApiError(`status ${httpStatus}`, {
            httpStatus,
            reasonCode,
            errorBody: `{"status":${httpStatus}}`,
          })
        },
        updateIssueComment: async () => {
          throw new Error('update should not run')
        },
      }

      await expect(upsertAgentRunReportComment(client, {
        repo: 'squne121/loop-protocol',
        targetNumber: 937,
        issueNumber: 937,
        prNumber: null,
        runId: 'run-937-001',
        payloadMarkdown: '<!-- agent_run_report:v1 start -->\n````json\n{"schema":"agent_run_report/v1","public_surface_kind":"github_issue_comment","public_safety":{"redaction_status":"clean","checked_by":"pnpm agent-run-report:check","validator_version":"1.0.0","checked_at":"2026-06-17T12:30:00.000Z","verdict":"pass","blocked_reasons":[]},"actor":{"type":"ai_agent","name":"Codex worker"},"authority":{"level":"non_authoritative","basis":"ai_self_report","evidence_refs":[]},"token_usage":{"availability":"unavailable","source":"none","prompt":null,"completion":null,"total":null},"manifest_refs":[],"evidence_refs":[],"commands_summary":[],"docs_read_refs":[]}\n````\n<!-- agent_run_report:v1 end -->',
      })).rejects.toMatchObject({
        httpStatus,
        reasonCode,
      })
    }
  })
})
