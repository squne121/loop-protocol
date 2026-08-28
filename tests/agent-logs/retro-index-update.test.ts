import { mkdtempSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { describe, expect, it } from 'vitest'

import { createValidReport } from './report-test-fixtures'
import { extractPayloadFromMarkdown } from '../../scripts/lib/agent-run-report-validation.mjs'
import { parseChecklistIssueNumbers, updateRetroIndex, verifyRetroIndexArtifact } from '../../scripts/agent-logs/update-retro-index.mjs'
import { buildAgentRunReportCommentBody } from '../../scripts/agent-logs/lib/github-comments.mjs'
import { renderValidatedPublicMarkdown } from '../../scripts/agent-logs/lib/validate-final-report.mjs'
import { buildRetroIndex, detectSchemaMigrationRequirement, normalizeRetrospectiveRunComment } from '../../scripts/agent-logs/lib/retro-index-builder.mjs'

// Issue #2238 Child 5: agent_retrospective_run_publication/v1 Issue comment
// fixture -- a distinct marker/schema from agent_run_report/v1 (see
// createSourceComment() above), routed to the additive retrospective_runs[]
// derived-index section rather than entries[].
function createRetrospectiveRunComment() {
  const envelope = {
    schema_version: 'agent_retrospective_run_publication/v1',
    repository_id: 'squne121/loop-protocol',
    target_issue: 2238,
    request_id: 'req-1',
    scope: 'repository',
    idempotency_key: `sha256:${'1'.repeat(64)}`,
    expected_previous_digest: null,
    parent_record_digest: null,
    run: {
      run_identity: {
        run_id: 'run-1',
        base_sha: 'a'.repeat(40),
        source_set_digest: 'd'.repeat(64),
        generated_at: '2026-08-22T00:00:00Z',
        runtime_version: 'agent-retrospective-persist/v1',
      },
      source_observations: [
        { source_type: 'repository', source_id: 'repository', source_status: 'complete', pagination_completeness: 'complete' },
      ],
    },
    candidate_records: [],
    delta_results: [{ finding_identity: `sha256:${'2'.repeat(64)}`, evaluation_status: 'classified', delta_status: 'new' }],
    publication_digest: `sha256:${'3'.repeat(64)}`,
  }
  const marker = `<!-- agent_retrospective_run:v1 repository_id=${envelope.repository_id} idempotency_key=${envelope.idempotency_key} -->`
  const fenced = `\`\`\`json\n${JSON.stringify(envelope, null, 2)}\n\`\`\``
  return {
    html_url: 'https://github.com/squne121/loop-protocol/issues/928#issuecomment-5000000001',
    body: `${marker}\n\n${fenced}\n`,
    linkedPrHints: [],
    linkedIssueHints: [928],
    branchHint: null,
  }
}

// Issue #2308: a structurally-invalid agent_retrospective_run_publication/v1
// envelope (publication_digest omitted) -- normalizeRetrospectiveRunComment()
// returns kind: 'blocked' for this, and buildRetroIndex() must record it in
// retrospective_runs_blocked[] instead of silently discarding it.
function createMalformedRetrospectiveRunComment() {
  const envelope = {
    schema_version: 'agent_retrospective_run_publication/v1',
    repository_id: 'squne121/loop-protocol',
    target_issue: 2238,
    request_id: 'req-malformed',
    scope: 'repository',
    idempotency_key: `sha256:${'5'.repeat(64)}`,
    expected_previous_digest: null,
    parent_record_digest: null,
    run: {
      run_identity: {
        run_id: 'run-malformed',
        base_sha: 'b'.repeat(40),
        source_set_digest: 'e'.repeat(64),
        generated_at: '2026-08-22T00:00:00Z',
        runtime_version: 'agent-retrospective-persist/v1',
      },
      source_observations: [
        { source_type: 'repository', source_id: 'repository', source_status: 'complete', pagination_completeness: 'complete' },
      ],
    },
    candidate_records: [],
    delta_results: [],
    // publication_digest intentionally omitted -- structurally invalid
  }
  const marker = `<!-- agent_retrospective_run:v1 repository_id=${envelope.repository_id} idempotency_key=${envelope.idempotency_key} -->`
  const fenced = `\`\`\`json\n${JSON.stringify(envelope, null, 2)}\n\`\`\``
  return {
    html_url: 'https://github.com/squne121/loop-protocol/issues/928#issuecomment-5000000010',
    body: `${marker}\n\n${fenced}\n`,
    linkedPrHints: [],
    linkedIssueHints: [928],
    branchHint: null,
  }
}

// Issue #2308 AC3: the first line carries the agent_retrospective_run:v1
// marker prefix but omits idempotency_key, so it fails strict
// RETROSPECTIVE_RUN_MARKER_LINE parsing while still being recognizably a
// retrospective-run marker (as opposed to an unrelated comment).
function createMarkerPrefixMalformedComment() {
  return {
    html_url: 'https://github.com/squne121/loop-protocol/issues/928#issuecomment-5000000011',
    body: '<!-- agent_retrospective_run:v1 repository_id=squne121/loop-protocol -->\n\nno idempotency_key on the marker line',
    linkedPrHints: [],
    linkedIssueHints: [928],
    branchHint: null,
  }
}

function createSourceComment() {
  const report = createValidReport()
  report.docs_read_refs = [
    {
      ref_kind: 'issue',
      ref: 'https://github.com/squne121/loop-protocol/issues/935',
      summary: 'Linked PR #955 validated',
    },
    {
      ref_kind: 'pull_request',
      ref: 'https://github.com/squne121/loop-protocol/pull/955',
      summary: 'Closes #935',
    },
  ]
  return {
    html_url: 'https://github.com/squne121/loop-protocol/issues/935#issuecomment-4713122667',
    body: buildAgentRunReportCommentBody({
      ownership: {
        repo: 'squne121/loop-protocol',
        issueNumber: 935,
        prNumber: null,
        runId: 'run-935-001',
      },
      payloadMarkdown: renderValidatedPublicMarkdown(report),
    }).body,
    linkedPrHints: [955],
    linkedIssueHints: [935],
    branchHint: 'worktree-issue-935-agent-run-report',
  }
}

function createBlockedSourceComment() {
  return {
    html_url: 'https://github.com/squne121/loop-protocol/issues/935#issuecomment-4713122669',
    body: '<!-- agent_run_report:v1 repo=squne121/loop-protocol issue=935 pr=955 run_id=run-935-blocked -->',
    linkedPrHints: [955],
    linkedIssueHints: [935],
    branchHint: 'worktree-issue-935-agent-run-report',
  }
}

describe('update-retro-index', () => {
  it('GIVEN dry-run mode WHEN updateRetroIndex builds and upserts THEN it returns summary-only output and create action', async () => {
    const client = {
      listIssueComments: async () => [],
      createIssueComment: async () => {
        throw new Error('create should not run in dry-run')
      },
      updateIssueComment: async () => {
        throw new Error('update should not run in dry-run')
      },
    }

    const result = await updateRetroIndex({
      repo: 'squne121/loop-protocol',
      parentIssue: 928,
      dryRun: true,
      issueCommentClient: client,
      sourceBundle: {
        childIssues: [935],
        sourceComments: [createSourceComment()],
        prMetadataByNumber: new Map([
          [955, {
            number: 955,
            body: 'Closes #935',
            mergeSha: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            headRefName: 'worktree-issue-935-agent-run-report',
          }],
        ]),
        associatedPrByMergeSha: new Map([
          ['aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 955],
        ]),
      },
    })

    expect(result.status).toBe('ok')
    expect(result.action).toBe('create')
    expect(result.summary).toMatchObject({
      generation_verdict: 'complete',
      entry_count: 1,
      orphan_count: 0,
      ambiguous_count: 0,
    })
    expect(JSON.stringify(result.summary)).not.toContain('agent_retro_index/v1')
  })

  it('GIVEN live mode without confirm-live WHEN updateRetroIndex runs THEN it fails closed before comment scanning', async () => {
    const client = {
      listIssueComments: async () => {
        throw new Error('list should not run')
      },
      createIssueComment: async () => {
        throw new Error('create should not run')
      },
      updateIssueComment: async () => {
        throw new Error('update should not run')
      },
    }

    await expect(updateRetroIndex({
      repo: 'squne121/loop-protocol',
      parentIssue: 928,
      dryRun: false,
      confirmLive: false,
      issueCommentClient: client,
      sourceBundle: {
        childIssues: [],
        sourceComments: [],
        prMetadataByNumber: new Map(),
        associatedPrByMergeSha: new Map(),
      },
    })).rejects.toThrow(/live posting requires --dry-run false and --confirm-live true/)
  })

  it('GIVEN a built artifact and summary WHEN verifyRetroIndexArtifact runs THEN canonical digest is revalidated without expanding schema keys', async () => {
    const client = {
      listIssueComments: async () => [],
      createIssueComment: async () => {
        throw new Error('create should not run in dry-run')
      },
      updateIssueComment: async () => {
        throw new Error('update should not run in dry-run')
      },
    }
    const result = await updateRetroIndex({
      repo: 'squne121/loop-protocol',
      parentIssue: 928,
      dryRun: true,
      issueCommentClient: client,
      sourceBundle: {
        childIssues: [935],
        sourceComments: [createSourceComment()],
        prMetadataByNumber: new Map([
          [955, {
            number: 955,
            body: 'Closes #935',
            mergeSha: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            headRefName: 'worktree-issue-935-agent-run-report',
          }],
        ]),
        associatedPrByMergeSha: new Map([
          ['aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 955],
        ]),
      },
    })
    const tempDir = mkdtempSync(join(tmpdir(), 'retro-index-'))
    const artifactPath = join(tempDir, 'agent-retro-index.json')
    const summaryPath = join(tempDir, 'agent-retro-index-summary.json')
    writeFileSync(artifactPath, `${JSON.stringify(result.index, null, 2)}\n`)
    writeFileSync(join(tempDir, 'agent-retro-index-source-set.json'), `${JSON.stringify(result.sourceCommentRefs, null, 2)}\n`)
    writeFileSync(summaryPath, `${JSON.stringify(result.summary, null, 2)}\n`)

    expect(verifyRetroIndexArtifact({
      artifactJsonPath: artifactPath,
      sourceSetJsonPath: join(tempDir, 'agent-retro-index-source-set.json'),
      summaryJsonPath: summaryPath,
    })).toMatchObject({
      status: 'ok',
      canonical_index_digest: result.summary.canonical_index_digest,
      source_comment_set_digest: result.summary.source_comment_set_digest,
      entry_count: 1,
    })
  })

  it('GIVEN a verified artifact bundle WHEN live update runs THEN the posted canonical payload stays byte-equivalent to the built artifact', async () => {
    const dryRunClient = {
      listIssueComments: async () => [],
      createIssueComment: async () => {
        throw new Error('create should not run in dry-run')
      },
      updateIssueComment: async () => {
        throw new Error('update should not run in dry-run')
      },
    }
    const built = await updateRetroIndex({
      repo: 'squne121/loop-protocol',
      parentIssue: 928,
      dryRun: true,
      issueCommentClient: dryRunClient,
      sourceBundle: {
        childIssues: [935],
        sourceComments: [createSourceComment()],
        prMetadataByNumber: new Map([
          [955, {
            number: 955,
            body: 'Closes #935',
            mergeSha: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            headRefName: 'worktree-issue-935-agent-run-report',
          }],
        ]),
        associatedPrByMergeSha: new Map([
          ['aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 955],
        ]),
      },
    })
    const createdBodies = []
    const liveClient = {
      listIssueComments: async () => [],
      createIssueComment: async ({ body }) => {
        createdBodies.push(body)
        return {
          id: 101,
          html_url: 'https://github.com/squne121/loop-protocol/issues/928#issuecomment-5000000000',
        }
      },
      updateIssueComment: async () => {
        throw new Error('update should not run')
      },
    }

    const result = await updateRetroIndex({
      repo: 'squne121/loop-protocol',
      parentIssue: 928,
      dryRun: false,
      confirmLive: true,
      issueCommentClient: liveClient,
      artifactBundle: {
        index: built.index,
        sourceCommentRefs: built.sourceCommentRefs,
        canonicalIndexDigest: built.canonical_index_digest,
        sourceCommentSetDigest: built.source_comment_set_digest,
        summary: built.summary,
      },
    })

    expect(result.status).toBe('ok')
    expect(createdBodies).toHaveLength(1)
    const extraction = extractPayloadFromMarkdown(createdBodies[0], 'agent_retro_index/v1')
    expect(extraction.ok).toBe(true)
    expect(extraction.payload).toEqual(built.index)
  })

  it('GIVEN a tampered summary digest WHEN verifyRetroIndexArtifact runs THEN the source-set artifact catches the mismatch', async () => {
    const client = {
      listIssueComments: async () => [],
      createIssueComment: async () => {
        throw new Error('create should not run in dry-run')
      },
      updateIssueComment: async () => {
        throw new Error('update should not run in dry-run')
      },
    }
    const result = await updateRetroIndex({
      repo: 'squne121/loop-protocol',
      parentIssue: 928,
      dryRun: true,
      issueCommentClient: client,
      sourceBundle: {
        childIssues: [935],
        sourceComments: [createSourceComment()],
        prMetadataByNumber: new Map([
          [955, {
            number: 955,
            body: 'Closes #935',
            mergeSha: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            headRefName: 'worktree-issue-935-agent-run-report',
          }],
        ]),
        associatedPrByMergeSha: new Map([
          ['aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 955],
        ]),
      },
    })
    const tempDir = mkdtempSync(join(tmpdir(), 'retro-index-'))
    const artifactPath = join(tempDir, 'agent-retro-index.json')
    const sourceSetPath = join(tempDir, 'agent-retro-index-source-set.json')
    const summaryPath = join(tempDir, 'agent-retro-index-summary.json')
    writeFileSync(artifactPath, `${JSON.stringify(result.index, null, 2)}\n`)
    writeFileSync(sourceSetPath, `${JSON.stringify(result.sourceCommentRefs, null, 2)}\n`)
    writeFileSync(summaryPath, `${JSON.stringify({
      ...result.summary,
      source_comment_set_digest: 'sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff',
    }, null, 2)}\n`)

    expect(() => verifyRetroIndexArtifact({
      artifactJsonPath: artifactPath,
      sourceSetJsonPath: sourceSetPath,
      summaryJsonPath: summaryPath,
    })).toThrow(/source-set artifact digest does not match the expected source-comment-set digest/)
  })

  it('GIVEN a blocked generation verdict WHEN updateRetroIndex runs THEN it fails closed before any upsert attempt', async () => {
    const client = {
      listIssueComments: async () => {
        throw new Error('list should not run')
      },
      createIssueComment: async () => {
        throw new Error('create should not run')
      },
      updateIssueComment: async () => {
        throw new Error('update should not run')
      },
    }

    const result = await updateRetroIndex({
      repo: 'squne121/loop-protocol',
      parentIssue: 928,
      dryRun: true,
      issueCommentClient: client,
      sourceBundle: {
        childIssues: [935],
        sourceComments: [createBlockedSourceComment()],
        prMetadataByNumber: new Map(),
        associatedPrByMergeSha: new Map(),
      },
    })

    expect(result.status).toBe('blocked')
    expect(result.action).toBeNull()
    expect(result.index.generation_verdict).toBe('blocked')
  })

  it('GIVEN parent child list variants WHEN parseChecklistIssueNumbers runs THEN checklist and URL bullets are all recognized', () => {
    expect(parseChecklistIssueNumbers([
      '- [ ] #123',
      '- [x] #456',
      '- #789',
      '- https://github.com/squne121/loop-protocol/issues/321',
    ].join('\n'))).toEqual([123, 456, 789, 321])
  })

  it('GIVEN an agent_retrospective_run_publication/v1 comment WHEN buildRetroIndex runs THEN retrospective_runs is populated additively without affecting entries', () => {
    // Uses buildRetroIndex() directly (not updateRetroIndex()'s live/dry-run
    // comment-body render+validate path -- that path's generic secret-like
    // scanner is out of this Issue's Allowed Paths and is exercised by
    // pre-existing entries[]/merge_sha coverage elsewhere, not by this
    // Issue's additive retrospective_runs[] change).
    const built = buildRetroIndex({
      sourceComments: [createSourceComment(), createRetrospectiveRunComment()],
      parentIssue: 928,
      prMetadataByNumber: new Map([
        [955, {
          number: 955,
          body: 'Closes #935',
          mergeSha: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
          headRefName: 'worktree-issue-935-agent-run-report',
        }],
      ]),
      associatedPrByMergeSha: new Map([
        ['aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 955],
      ]),
      parentChildIssueNumbers: [935],
    })

    // entries[] (agent_run_report/v1) is unaffected by the additive change
    expect(built.index.generation_verdict).toBe('complete')
    expect(built.index.entries).toHaveLength(1)

    // retrospective_runs[] carries the additive derived summary
    expect(built.index.retrospective_runs).toHaveLength(1)
    expect(built.index.retrospective_runs[0]).toMatchObject({
      run_comment_url: 'https://github.com/squne121/loop-protocol/issues/928#issuecomment-5000000001',
      base_sha: 'a'.repeat(40),
      source_set_digest: 'd'.repeat(64),
      candidate_count: 0,
      delta_summary: 'new:1',
    })
    // Issue #2238 P0-7 fix_delta: run_digest references the envelope's own
    // verified publication_digest directly, not a separately-computed
    // digest of pretty-printed JSON.
    expect(built.index.retrospective_runs[0].run_digest).toBe(`sha256:${'3'.repeat(64)}`)

    // the additive key does not trigger the schema-migration guard
    expect(detectSchemaMigrationRequirement(built.index)).toBeNull()
  })

  it('GIVEN a retrospective-run comment missing publication_digest WHEN normalizeRetrospectiveRunComment runs THEN it is blocked (Issue #2238 P0-7)', () => {
    const envelope = {
      schema_version: 'agent_retrospective_run_publication/v1',
      repository_id: 'squne121/loop-protocol',
      target_issue: 2238,
      request_id: 'req-no-digest',
      scope: 'repository',
      idempotency_key: `sha256:${'1'.repeat(64)}`,
      expected_previous_digest: null,
      parent_record_digest: null,
      run: {
        run_identity: {
          run_id: 'run-1',
          base_sha: 'a'.repeat(40),
          source_set_digest: 'd'.repeat(64),
          generated_at: '2026-08-22T00:00:00Z',
          runtime_version: 'agent-retrospective-persist/v1',
        },
        source_observations: [
          { source_type: 'repository', source_id: 'repository', source_status: 'complete', pagination_completeness: 'complete' },
        ],
      },
      candidate_records: [],
      delta_results: [],
      // publication_digest intentionally omitted
    }
    const marker = `<!-- agent_retrospective_run:v1 repository_id=${envelope.repository_id} idempotency_key=${envelope.idempotency_key} -->`
    const fenced = `\`\`\`json\n${JSON.stringify(envelope, null, 2)}\n\`\`\``
    const blocked = normalizeRetrospectiveRunComment({
      html_url: 'https://github.com/squne121/loop-protocol/issues/928#issuecomment-5000000003',
      body: `${marker}\n\n${fenced}\n`,
    })

    expect(blocked.kind).toBe('blocked')
    expect(blocked.reason).toBe('retrospective_run_publication_digest_missing')
  })

  it('GIVEN a malformed retrospective-run marker WHEN normalizeRetrospectiveRunComment runs THEN it is blocked without becoming an entries[]/orphan/ambiguous side effect', () => {
    const blocked = normalizeRetrospectiveRunComment({
      html_url: 'https://github.com/squne121/loop-protocol/issues/928#issuecomment-5000000002',
      body: `<!-- agent_retrospective_run:v1 repository_id=squne121/loop-protocol idempotency_key=sha256:${'1'.repeat(64)} -->\n\nno fenced json block here`,
    })

    expect(blocked.kind).toBe('blocked')
    expect(blocked.reason).toBe('retrospective_run_payload_unparsable')
  })

  it('GIVEN a comment with no retrospective-run marker WHEN normalizeRetrospectiveRunComment runs THEN it is ignored (not routed to retrospective_runs)', () => {
    const ignored = normalizeRetrospectiveRunComment(createSourceComment())
    expect(ignored.kind).toBe('ignored')
  })

  it('GIVEN a marker prefix present but idempotency_key missing WHEN normalizeRetrospectiveRunComment runs THEN it is blocked as retrospective_run_marker_malformed rather than ignored (Issue #2308 AC3)', () => {
    const blocked = normalizeRetrospectiveRunComment(createMarkerPrefixMalformedComment())
    expect(blocked.kind).toBe('blocked')
    expect(blocked.reason).toBe('retrospective_run_marker_malformed')
  })

  it('GIVEN a v1 marker with no delimiter after the version (e.g. v1repository_id=...) WHEN normalizeRetrospectiveRunComment runs THEN it is blocked as retrospective_run_marker_malformed rather than ignored (Issue #2366 fix_delta: \\b does not fire between two word characters)', () => {
    const blocked = normalizeRetrospectiveRunComment({
      html_url: 'https://github.com/squne121/loop-protocol/issues/928#issuecomment-5000000012',
      body: '<!-- agent_retrospective_run:v1repository_id=squne121/loop-protocol idempotency_key=sha256:abc -->\n\nno space after v1',
      linkedPrHints: [],
      linkedIssueHints: [928],
      branchHint: null,
    })
    expect(blocked.kind).toBe('blocked')
    expect(blocked.reason).toBe('retrospective_run_marker_malformed')
  })

  it('GIVEN a v1 marker followed by an underscore (e.g. v1_repository_id=...) WHEN normalizeRetrospectiveRunComment runs THEN it is blocked as retrospective_run_marker_malformed rather than ignored (Issue #2366 fix_delta: \\b does not fire between "1" and "_")', () => {
    const blocked = normalizeRetrospectiveRunComment({
      html_url: 'https://github.com/squne121/loop-protocol/issues/928#issuecomment-5000000013',
      body: '<!-- agent_retrospective_run:v1_repository_id=squne121/loop-protocol idempotency_key=sha256:abc -->\n\nunderscore after v1',
      linkedPrHints: [],
      linkedIssueHints: [928],
      branchHint: null,
    })
    expect(blocked.kind).toBe('blocked')
    expect(blocked.reason).toBe('retrospective_run_marker_malformed')
  })

  it('GIVEN a v10 marker (a different/future marker version, not v1) WHEN normalizeRetrospectiveRunComment runs THEN it stays ignored rather than being misclassified as a malformed v1 marker (Issue #2366 fix_delta negative control)', () => {
    const ignored = normalizeRetrospectiveRunComment({
      html_url: 'https://github.com/squne121/loop-protocol/issues/928#issuecomment-5000000014',
      body: '<!-- agent_retrospective_run:v10 repository_id=squne121/loop-protocol idempotency_key=sha256:abc -->\n\nfuture marker version',
      linkedPrHints: [],
      linkedIssueHints: [928],
      branchHint: null,
    })
    expect(ignored.kind).toBe('ignored')
  })

  it('GIVEN a valid entry and a structurally-invalid retrospective-run comment WHEN buildRetroIndex runs THEN entries[] is preserved, retrospective_runs_blocked records the malformed run, and generation_verdict becomes partial (Issue #2308 AC1/AC2/AC5)', () => {
    const malformedRunComment = createMalformedRetrospectiveRunComment()
    const built = buildRetroIndex({
      sourceComments: [createSourceComment(), malformedRunComment],
      parentIssue: 928,
      prMetadataByNumber: new Map([
        [955, {
          number: 955,
          body: 'Closes #935',
          mergeSha: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
          headRefName: 'worktree-issue-935-agent-run-report',
        }],
      ]),
      associatedPrByMergeSha: new Map([
        ['aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 955],
      ]),
      parentChildIssueNumbers: [935],
    })

    // entries[] (agent_run_report/v1) is not lost because of the malformed
    // retrospective-run comment
    expect(built.index.entries).toHaveLength(1)
    expect(built.index.generation_verdict).toBe('partial')

    expect(built.index.retrospective_runs_blocked).toEqual([
      {
        run_comment_url: malformedRunComment.html_url,
        reason: 'retrospective_run_publication_digest_missing',
      },
    ])

    // existing blockedReasons[]/entries[] agent_run_report/v1 blocked
    // semantics are unaffected by this additive change
    expect(built.blockedReasons).toHaveLength(0)

    // the additive key does not trigger the schema-migration guard
    expect(detectSchemaMigrationRequirement(built.index)).toBeNull()
  })

  it('GIVEN a retrospective-run comment whose marker prefix fails strict parsing WHEN buildRetroIndex runs THEN it is recorded in retrospective_runs_blocked with reason retrospective_run_marker_malformed and generation_verdict becomes partial (Issue #2308 AC3)', () => {
    const malformedMarkerComment = createMarkerPrefixMalformedComment()
    const built = buildRetroIndex({
      sourceComments: [malformedMarkerComment],
      parentIssue: 928,
    })

    expect(built.index.retrospective_runs_blocked).toEqual([
      {
        run_comment_url: malformedMarkerComment.html_url,
        reason: 'retrospective_run_marker_malformed',
      },
    ])
    expect(built.index.generation_verdict).toBe('partial')
    expect(built.index.entries).toHaveLength(0)
  })

  it('GIVEN only a valid retrospective-run comment and no malformed runs WHEN buildRetroIndex runs THEN retrospective_runs_blocked stays empty and generation_verdict remains complete (regression: additive field does not degrade the normal-path verdict)', () => {
    const built = buildRetroIndex({
      sourceComments: [createSourceComment(), createRetrospectiveRunComment()],
      parentIssue: 928,
      prMetadataByNumber: new Map([
        [955, {
          number: 955,
          body: 'Closes #935',
          mergeSha: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
          headRefName: 'worktree-issue-935-agent-run-report',
        }],
      ]),
      associatedPrByMergeSha: new Map([
        ['aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 955],
      ]),
      parentChildIssueNumbers: [935],
    })

    expect(built.index.retrospective_runs_blocked).toEqual([])
    expect(built.index.generation_verdict).toBe('complete')
  })
})
