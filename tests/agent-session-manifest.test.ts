/**
 * agent-session-manifest.test.ts
 *
 * JSON Schema Draft 2020-12: docs/schemas/agent-session-manifest.schema.json
 * の valid / invalid fixture バリデーションテスト。
 *
 * ajv / zod は未インストールのため、スキーマの主要制約を TypeScript で直接検証する。
 * JSON Schema ファイルの存在・構造は fs で確認する。
 */
import { readFileSync, existsSync } from 'fs'
import { resolve, dirname } from 'path'
import { fileURLToPath } from 'url'
import { describe, expect, it } from 'vitest'

const __dirname = dirname(fileURLToPath(import.meta.url))
const SCHEMA_PATH = resolve(__dirname, '../docs/schemas/agent-session-manifest.schema.json')
const EXAMPLES_DIR = resolve(__dirname, '../docs/schemas/examples')

// ---- schema object types ----

type ActorType = 'ai_agent' | 'human' | 'github_action'
type MainLoopPhase =
  | 'issue_create'
  | 'issue_review'
  | 'impl'
  | 'pr_open'
  | 'pr_review'
  | 'merge'
  | 'followup_create'
type LedgerPhase =
  | 'followup_issue_materialization'
  | 'issue_contract_preflight'
  | 'implementation'
  | 'post_commit_verification'
  | 'pr_body_update'
  | 'semantic_review'
  | 'pre_merge_judgment'
  | 'github_merge_event'
  | null
type VisibilityKind = 'public_github_comment' | 'private_artifact' | 'local_only'
type SourceKind = 'github_comment' | 'ci_check' | 'hook_jsonl' | 'artifact' | 'transcript' | 'local_file'

interface EvidenceItem {
  source_kind: SourceKind
  source_ref: string
  source_sha256?: string | null
  visibility?: VisibilityKind
}

interface AgentSessionManifest {
  schema: string
  manifest_id: string
  recorded_at: string
  repository: string
  head_sha?: string | null
  issue_number?: number | null
  pr_number?: number | null
  commit_sha?: string | null
  actor: {
    type: ActorType
    name: string
    session_id?: string | null
  }
  phase: {
    main_loop: MainLoopPhase
    ledger_phase?: LedgerPhase
    phase_instance_id: string
  }
  token_usage?: {
    availability: 'measured' | 'estimated' | 'unavailable'
    source: 'provider_api' | 'tool_log' | 'entire_cli' | 'manual_report' | 'none'
    prompt?: number | null
    completion?: number | null
    total?: number | null
  }
  invoked_subagents?: Array<{
    name: string
    count: number
    duration_ms?: number | null
  }>
  verification?: {
    overall: 'pass' | 'fail' | 'partial' | 'blocked' | 'not_applicable'
    skipped_count: number
    fallback_detected: boolean
    ac_results: Array<{
      ac: string
      verdict: 'pass' | 'fail' | 'skip' | 'blocked' | 'not_applicable'
      command?: string | null
      exit_code?: number | null
      artifact_ref?: string | null
      waiver_ref?: string | null
    }>
  }
  evidence?: EvidenceItem[]
  hook_event?: {
    event_type?: 'SubagentStart' | 'SubagentStop' | 'PostToolUse' | 'Stop' | 'PreToolUse'
    hook_id?: string | null
    triggered_at?: string | null
  }
  sanitization_status?: 'not_sanitized' | 'sanitized' | 'sanitization_failed'
  redaction: {
    raw_transcript_included: boolean
    local_paths_included: boolean
    secret_scan_status: 'not_applicable' | 'clean' | 'flagged'
  }
  human_intervention?: {
    required: boolean
    type: 'none' | 'approval' | 'correction' | 'escalation'
    summary?: string | null
  }
  next_action_issue?: number | null
}

// ---- validation helpers ----

const MANIFEST_ID_PATTERN =
  /^asm-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
const SHA40_PATTERN = /^[0-9a-f]{40}$/
const PHASE_INSTANCE_PATTERN = /^issue-[0-9]+:[a-z_]+:[0-9]{3}$/

const MAIN_LOOP_PHASES: MainLoopPhase[] = [
  'issue_create',
  'issue_review',
  'impl',
  'pr_open',
  'pr_review',
  'merge',
  'followup_create',
]
const LEDGER_PHASES: LedgerPhase[] = [
  'followup_issue_materialization',
  'issue_contract_preflight',
  'implementation',
  'post_commit_verification',
  'pr_body_update',
  'semantic_review',
  'pre_merge_judgment',
  'github_merge_event',
  null,
]

interface ValidationResult {
  valid: boolean
  errors: string[]
}

function validateManifest(data: unknown): ValidationResult {
  const errors: string[] = []

  if (typeof data !== 'object' || data === null) {
    return { valid: false, errors: ['root must be object'] }
  }

  const m = data as Record<string, unknown>

  // Required fields
  const requiredFields = ['schema', 'manifest_id', 'recorded_at', 'repository', 'actor', 'phase', 'redaction']
  for (const field of requiredFields) {
    if (!(field in m)) {
      errors.push(`required field missing: ${field}`)
    }
  }

  // schema const
  if (m['schema'] !== 'agent_session_manifest/v1') {
    errors.push(`schema must be "agent_session_manifest/v1", got: ${String(m['schema'])}`)
  }

  // manifest_id pattern
  if (typeof m['manifest_id'] === 'string') {
    if (!MANIFEST_ID_PATTERN.test(m['manifest_id'])) {
      errors.push(`manifest_id pattern invalid: ${m['manifest_id']}`)
    }
  } else {
    errors.push('manifest_id must be string')
  }

  // head_sha pattern (nullable)
  if ('head_sha' in m && m['head_sha'] !== null) {
    if (typeof m['head_sha'] !== 'string' || !SHA40_PATTERN.test(m['head_sha'])) {
      errors.push(`head_sha must be 40-hex string or null, got: ${String(m['head_sha'])}`)
    }
  }

  // commit_sha pattern (nullable)
  if ('commit_sha' in m && m['commit_sha'] !== null) {
    if (typeof m['commit_sha'] !== 'string' || !SHA40_PATTERN.test(m['commit_sha'])) {
      errors.push(`commit_sha must be 40-hex string or null, got: ${String(m['commit_sha'])}`)
    }
  }

  // actor
  if (typeof m['actor'] === 'object' && m['actor'] !== null) {
    const actor = m['actor'] as Record<string, unknown>
    if (!['ai_agent', 'human', 'github_action'].includes(actor['type'] as string)) {
      errors.push(`actor.type must be ai_agent|human|github_action, got: ${String(actor['type'])}`)
    }
    if (typeof actor['name'] !== 'string') {
      errors.push('actor.name must be string')
    }
  }

  // phase
  if (typeof m['phase'] === 'object' && m['phase'] !== null) {
    const phase = m['phase'] as Record<string, unknown>
    if (!MAIN_LOOP_PHASES.includes(phase['main_loop'] as MainLoopPhase)) {
      errors.push(`phase.main_loop must be one of 7 enum values, got: ${String(phase['main_loop'])}`)
    }
    if ('ledger_phase' in phase && phase['ledger_phase'] !== null) {
      const nonNullLedger = LEDGER_PHASES.filter((p): p is Exclude<LedgerPhase, null> => p !== null)
      if (!nonNullLedger.includes(phase['ledger_phase'] as Exclude<LedgerPhase, null>)) {
        errors.push(`phase.ledger_phase enum invalid: ${String(phase['ledger_phase'])}`)
      }
    }
    if (typeof phase['phase_instance_id'] === 'string') {
      if (!PHASE_INSTANCE_PATTERN.test(phase['phase_instance_id'])) {
        errors.push(`phase.phase_instance_id pattern invalid: ${String(phase['phase_instance_id'])}`)
      }
    } else {
      errors.push('phase.phase_instance_id must be string')
    }
  }

  // evidence: visibility constraint
  if ('evidence' in m && Array.isArray(m['evidence'])) {
    const evidence = m['evidence'] as Array<Record<string, unknown>>
    for (const [idx, item] of evidence.entries()) {
      if (item['visibility'] === 'public_github_comment') {
        if (item['source_kind'] === 'transcript' || item['source_kind'] === 'local_file') {
          errors.push(
            `evidence[${idx}]: source_kind "${String(item['source_kind'])}" is forbidden when visibility is public_github_comment`,
          )
        }
      }
    }
  }

  return { valid: errors.length === 0, errors }
}

// ---- fixtures ----

const validFixtures: Array<{ name: string; data: AgentSessionManifest }> = [
  {
    name: 'issue_review phase (minimal)',
    data: {
      schema: 'agent_session_manifest/v1',
      manifest_id: 'asm-12345678-1234-4123-89ab-123456789abc',
      recorded_at: '2026-05-24T10:00:00Z',
      repository: 'squne121/loop-protocol',
      head_sha: null,
      issue_number: 243,
      pr_number: null,
      commit_sha: null,
      actor: { type: 'ai_agent', name: 'issue-reviewer', session_id: 'session-001' },
      phase: {
        main_loop: 'issue_review',
        ledger_phase: 'issue_contract_preflight',
        phase_instance_id: 'issue-243:issue_review:001',
      },
      token_usage: { availability: 'unavailable', source: 'none', prompt: null, completion: null, total: null },
      invoked_subagents: [{ name: 'issue-reviewer', count: 1, duration_ms: null }],
      verification: { overall: 'not_applicable', skipped_count: 0, fallback_detected: false, ac_results: [] },
      evidence: [
        {
          source_kind: 'github_comment',
          source_ref: 'https://github.com/squne121/loop-protocol/issues/243#issuecomment-001',
          source_sha256: null,
          visibility: 'public_github_comment',
        },
      ],
      human_intervention: { required: false, type: 'none', summary: null },
      next_action_issue: null,
      redaction: { raw_transcript_included: false, local_paths_included: false, secret_scan_status: 'not_applicable' },
    },
  },
  {
    name: 'impl phase (implementation manifest, 40-hex head_sha)',
    data: {
      schema: 'agent_session_manifest/v1',
      manifest_id: 'asm-abcdef12-abcd-4abc-89ab-abcdef123456',
      recorded_at: '2026-05-24T12:00:00Z',
      repository: 'squne121/loop-protocol',
      head_sha: 'abcdef1234567890abcdef1234567890abcdef12',
      issue_number: 243,
      pr_number: 314,
      commit_sha: 'abcdef1234567890abcdef1234567890abcdef12',
      actor: { type: 'ai_agent', name: 'implementation-worker', session_id: 'session-002' },
      phase: {
        main_loop: 'impl',
        ledger_phase: 'implementation',
        phase_instance_id: 'issue-243:impl:001',
      },
      token_usage: { availability: 'measured', source: 'provider_api', prompt: 12000, completion: 3500, total: 15500 },
      invoked_subagents: [],
      verification: {
        overall: 'pass',
        skipped_count: 0,
        fallback_detected: false,
        ac_results: [
          {
            ac: 'AC1',
            verdict: 'pass',
            command: "test -f docs/schemas/agent-session-manifest.md && rg '^schema_version: v1$'",
            exit_code: 0,
            artifact_ref: null,
            waiver_ref: null,
          },
        ],
      },
      evidence: [
        {
          source_kind: 'github_comment',
          source_ref: 'https://github.com/squne121/loop-protocol/pull/314#issuecomment-impl-001',
          source_sha256: null,
          visibility: 'public_github_comment',
        },
      ],
      hook_event: { event_type: 'SubagentStop', hook_id: 'hook-001', triggered_at: '2026-05-24T12:00:01Z' },
      sanitization_status: 'clean',
      human_intervention: { required: false, type: 'none', summary: null },
      next_action_issue: null,
      redaction: { raw_transcript_included: false, local_paths_included: false, secret_scan_status: 'clean' },
    },
  },
  {
    name: 'merge phase by human (actor.type: human, session_id: null)',
    data: {
      schema: 'agent_session_manifest/v1',
      manifest_id: 'asm-11111111-2222-4333-89ab-444444444444',
      recorded_at: '2026-05-24T15:00:00Z',
      repository: 'squne121/loop-protocol',
      head_sha: '1234567890abcdef1234567890abcdef12345678',
      issue_number: 243,
      pr_number: 314,
      commit_sha: '1234567890abcdef1234567890abcdef12345678',
      actor: { type: 'human', name: 'human', session_id: null },
      phase: {
        main_loop: 'merge',
        ledger_phase: 'github_merge_event',
        phase_instance_id: 'issue-243:merge:001',
      },
      token_usage: { availability: 'unavailable', source: 'none', prompt: null, completion: null, total: null },
      invoked_subagents: [],
      verification: { overall: 'not_applicable', skipped_count: 0, fallback_detected: false, ac_results: [] },
      evidence: [
        {
          source_kind: 'github_comment',
          source_ref: 'https://github.com/squne121/loop-protocol/pull/314',
          source_sha256: null,
          visibility: 'public_github_comment',
        },
      ],
      human_intervention: { required: true, type: 'approval', summary: '人間が GitHub UI からマージを実行' },
      next_action_issue: null,
      redaction: { raw_transcript_included: false, local_paths_included: false, secret_scan_status: 'not_applicable' },
    },
  },
  {
    name: 'private_artifact evidence with transcript source_kind (allowed)',
    data: {
      schema: 'agent_session_manifest/v1',
      manifest_id: 'asm-55555555-5555-4555-89ab-555555555555',
      recorded_at: '2026-05-24T16:00:00Z',
      repository: 'squne121/loop-protocol',
      head_sha: null,
      actor: { type: 'ai_agent', name: 'implementation-worker', session_id: null },
      phase: {
        main_loop: 'impl',
        ledger_phase: 'post_commit_verification',
        phase_instance_id: 'issue-243:impl:002',
      },
      evidence: [
        {
          source_kind: 'transcript',
          source_ref: '/home/user/.claude/transcripts/session-001.jsonl',
          source_sha256: null,
          visibility: 'private_artifact',
        },
      ],
      redaction: { raw_transcript_included: false, local_paths_included: true, secret_scan_status: 'clean' },
    },
  },
]

const invalidFixtures: Array<{ name: string; data: unknown; expectErrorContaining: string }> = [
  {
    name: 'manifest_id with old asm-date-seq format (not UUIDv4)',
    data: {
      schema: 'agent_session_manifest/v1',
      manifest_id: 'asm-20260523-001',
      recorded_at: '2026-05-24T10:00:00Z',
      repository: 'squne121/loop-protocol',
      actor: { type: 'ai_agent', name: 'implementation-worker', session_id: null },
      phase: { main_loop: 'impl', ledger_phase: 'implementation', phase_instance_id: 'issue-243:impl:001' },
      redaction: { raw_transcript_included: false, local_paths_included: false, secret_scan_status: 'not_applicable' },
    },
    expectErrorContaining: 'manifest_id pattern invalid',
  },
  {
    name: 'head_sha with short SHA (not 40-hex)',
    data: {
      schema: 'agent_session_manifest/v1',
      manifest_id: 'asm-12345678-1234-4123-89ab-123456789abc',
      recorded_at: '2026-05-24T10:00:00Z',
      repository: 'squne121/loop-protocol',
      head_sha: 'abc1234',
      actor: { type: 'ai_agent', name: 'implementation-worker', session_id: null },
      phase: { main_loop: 'impl', ledger_phase: 'implementation', phase_instance_id: 'issue-243:impl:001' },
      redaction: { raw_transcript_included: false, local_paths_included: false, secret_scan_status: 'not_applicable' },
    },
    expectErrorContaining: 'head_sha must be 40-hex string or null',
  },
  {
    name: 'evidence source_kind: transcript with visibility: public_github_comment (forbidden)',
    data: {
      schema: 'agent_session_manifest/v1',
      manifest_id: 'asm-12345678-1234-4123-89ab-123456789abc',
      recorded_at: '2026-05-24T10:00:00Z',
      repository: 'squne121/loop-protocol',
      actor: { type: 'ai_agent', name: 'implementation-worker', session_id: null },
      phase: { main_loop: 'impl', ledger_phase: 'implementation', phase_instance_id: 'issue-243:impl:001' },
      evidence: [
        {
          source_kind: 'transcript',
          source_ref: '/home/user/.claude/transcripts/session.jsonl',
          source_sha256: null,
          visibility: 'public_github_comment',
        },
      ],
      redaction: { raw_transcript_included: true, local_paths_included: true, secret_scan_status: 'flagged' },
    },
    expectErrorContaining: 'forbidden when visibility is public_github_comment',
  },
  {
    name: 'evidence source_kind: local_file with visibility: public_github_comment (forbidden)',
    data: {
      schema: 'agent_session_manifest/v1',
      manifest_id: 'asm-12345678-1234-4123-89ab-123456789abc',
      recorded_at: '2026-05-24T10:00:00Z',
      repository: 'squne121/loop-protocol',
      actor: { type: 'ai_agent', name: 'implementation-worker', session_id: null },
      phase: { main_loop: 'impl', ledger_phase: 'implementation', phase_instance_id: 'issue-243:impl:001' },
      evidence: [
        {
          source_kind: 'local_file',
          source_ref: '/home/user/report.txt',
          source_sha256: null,
          visibility: 'public_github_comment',
        },
      ],
      redaction: { raw_transcript_included: false, local_paths_included: true, secret_scan_status: 'flagged' },
    },
    expectErrorContaining: 'forbidden when visibility is public_github_comment',
  },
  {
    name: 'required field redaction missing',
    data: {
      schema: 'agent_session_manifest/v1',
      manifest_id: 'asm-12345678-1234-4123-89ab-123456789abc',
      recorded_at: '2026-05-24T10:00:00Z',
      repository: 'squne121/loop-protocol',
      actor: { type: 'ai_agent', name: 'implementation-worker', session_id: null },
      phase: { main_loop: 'impl', ledger_phase: 'implementation', phase_instance_id: 'issue-243:impl:001' },
      // redaction intentionally missing
    },
    expectErrorContaining: 'required field missing: redaction',
  },
  {
    name: 'schema const wrong (v2 instead of v1)',
    data: {
      schema: 'agent_session_manifest/v2',
      manifest_id: 'asm-12345678-1234-4123-89ab-123456789abc',
      recorded_at: '2026-05-24T10:00:00Z',
      repository: 'squne121/loop-protocol',
      actor: { type: 'ai_agent', name: 'implementation-worker', session_id: null },
      phase: { main_loop: 'impl', ledger_phase: 'implementation', phase_instance_id: 'issue-243:impl:001' },
      redaction: { raw_transcript_included: false, local_paths_included: false, secret_scan_status: 'not_applicable' },
    },
    expectErrorContaining: 'schema must be "agent_session_manifest/v1"',
  },
  {
    name: 'phase.main_loop invalid enum value',
    data: {
      schema: 'agent_session_manifest/v1',
      manifest_id: 'asm-12345678-1234-4123-89ab-123456789abc',
      recorded_at: '2026-05-24T10:00:00Z',
      repository: 'squne121/loop-protocol',
      actor: { type: 'ai_agent', name: 'implementation-worker', session_id: null },
      phase: { main_loop: 'unknown_phase', ledger_phase: 'implementation', phase_instance_id: 'issue-243:impl:001' },
      redaction: { raw_transcript_included: false, local_paths_included: false, secret_scan_status: 'not_applicable' },
    },
    expectErrorContaining: 'phase.main_loop must be one of 7 enum values',
  },
]

// ---- tests ----

describe('agent-session-manifest schema file', () => {
  it('GIVEN schema JSON file WHEN checking existence THEN file exists at docs/schemas/agent-session-manifest.schema.json', () => {
    expect(existsSync(SCHEMA_PATH)).toBe(true)
  })

  it('GIVEN schema JSON file WHEN parsing THEN it has correct $schema and title', () => {
    const raw = readFileSync(SCHEMA_PATH, 'utf-8')
    const schema = JSON.parse(raw) as Record<string, unknown>
    expect(schema['$schema']).toBe('https://json-schema.org/draft/2020-12/schema')
    expect(schema['title']).toBe('agent_session_manifest/v1')
  })

  it('GIVEN schema JSON file WHEN checking phase.main_loop enum THEN 7 values are defined (AC3)', () => {
    const raw = readFileSync(SCHEMA_PATH, 'utf-8')
    const schema = JSON.parse(raw) as Record<string, unknown>
    const props = schema['properties'] as Record<string, unknown>
    const phase = props['phase'] as Record<string, unknown>
    const phaseProps = phase['properties'] as Record<string, unknown>
    const mainLoop = phaseProps['main_loop'] as Record<string, unknown>
    const enumValues = mainLoop['enum'] as string[]
    expect(enumValues).toHaveLength(7)
    expect(enumValues).toContain('issue_create')
    expect(enumValues).toContain('issue_review')
    expect(enumValues).toContain('impl')
    expect(enumValues).toContain('pr_open')
    expect(enumValues).toContain('pr_review')
    expect(enumValues).toContain('merge')
    expect(enumValues).toContain('followup_create')
  })

  it('GIVEN schema JSON file WHEN checking manifest_id THEN pattern is asm-UUIDv4 (AC16)', () => {
    const raw = readFileSync(SCHEMA_PATH, 'utf-8')
    const schema = JSON.parse(raw) as Record<string, unknown>
    const props = schema['properties'] as Record<string, unknown>
    const manifestId = props['manifest_id'] as Record<string, unknown>
    const pattern = manifestId['pattern'] as string
    expect(pattern).toContain('asm-')
    // UUIDv4 version byte must be 4
    expect(pattern).toContain('4[0-9a-f]{3}')
  })

  it('GIVEN schema JSON file WHEN checking head_sha THEN 40-hex pattern is defined (AC16)', () => {
    const raw = readFileSync(SCHEMA_PATH, 'utf-8')
    const schema = JSON.parse(raw) as Record<string, unknown>
    const props = schema['properties'] as Record<string, unknown>
    const headSha = props['head_sha'] as Record<string, unknown>
    const oneOf = headSha['oneOf'] as Array<Record<string, unknown>>
    const stringDef = oneOf.find((o) => o['type'] === 'string')
    expect(stringDef).toBeDefined()
    expect((stringDef as Record<string, unknown>)['pattern']).toBe('^[0-9a-f]{40}$')
  })

  it('GIVEN schema JSON file WHEN checking ledger_phase THEN it is enum scalar (AC17)', () => {
    const raw = readFileSync(SCHEMA_PATH, 'utf-8')
    const schema = JSON.parse(raw) as Record<string, unknown>
    const props = schema['properties'] as Record<string, unknown>
    const phase = props['phase'] as Record<string, unknown>
    const phaseProps = phase['properties'] as Record<string, unknown>
    const ledgerPhase = phaseProps['ledger_phase'] as Record<string, unknown>
    // ledger_phase is oneOf [enum string, null]
    const oneOf = ledgerPhase['oneOf'] as Array<Record<string, unknown>>
    const stringDef = oneOf.find((o) => o['type'] === 'string')
    expect(stringDef).toBeDefined()
    expect(stringDef!['enum']).toBeDefined()
    expect(Array.isArray(stringDef!['enum'])).toBe(true)
  })

  it('GIVEN schema JSON file WHEN checking evidence.visibility constraint THEN if/then rule for public_github_comment is present (AC14)', () => {
    const raw = readFileSync(SCHEMA_PATH, 'utf-8')
    const schema = JSON.parse(raw) as Record<string, unknown>
    const props = schema['properties'] as Record<string, unknown>
    const evidence = props['evidence'] as Record<string, unknown>
    const items = evidence['items'] as Record<string, unknown>
    expect(items['if']).toBeDefined()
    expect(items['then']).toBeDefined()
  })
})

describe('agent-session-manifest examples directory', () => {
  it('GIVEN examples directory THEN valid fixtures exist', () => {
    const validFiles = [
      'valid-issue-review.yaml',
      'valid-impl-phase.yaml',
      'valid-merge-human.yaml',
    ]
    for (const f of validFiles) {
      expect(existsSync(`${EXAMPLES_DIR}/${f}`), `expected ${f} to exist`).toBe(true)
    }
  })

  it('GIVEN examples directory THEN invalid fixtures exist', () => {
    const invalidFiles = [
      'invalid-bad-manifest-id.yaml',
      'invalid-bad-head-sha.yaml',
      'invalid-transcript-in-public-comment.yaml',
      'invalid-missing-required.yaml',
      'invalid-wrong-schema-const.yaml',
    ]
    for (const f of invalidFiles) {
      expect(existsSync(`${EXAMPLES_DIR}/${f}`), `expected ${f} to exist`).toBe(true)
    }
  })
})

describe('agent-session-manifest valid fixtures', () => {
  for (const fixture of validFixtures) {
    it(`GIVEN valid manifest "${fixture.name}" WHEN validating THEN result is valid`, () => {
      const result = validateManifest(fixture.data)
      expect(result.errors).toEqual([])
      expect(result.valid).toBe(true)
    })
  }
})

describe('agent-session-manifest invalid fixtures', () => {
  for (const fixture of invalidFixtures) {
    it(`GIVEN invalid manifest "${fixture.name}" WHEN validating THEN result is invalid with expected error`, () => {
      const result = validateManifest(fixture.data)
      expect(result.valid).toBe(false)
      const hasExpectedError = result.errors.some((e) => e.includes(fixture.expectErrorContaining))
      expect(hasExpectedError, `expected error containing "${fixture.expectErrorContaining}" but got: ${result.errors.join(', ')}`).toBe(true)
    })
  }
})
