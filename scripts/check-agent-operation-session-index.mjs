#!/usr/bin/env node
// check-agent-operation-session-index.mjs
//
// Fail-closed checker for agent_operation_session_index/v1 (Issue #1405, parent #1153).
//
// Responsibilities:
//   1. schema validation (Ajv 2020-12, closed shape via additionalProperties:false)
//   2. semantic invariant checks (target.kind/URL alignment, run_id/target/parent-issue
//      cross reference, github_event_ref.kind <-> operation.kind closed mapping,
//      raw_values_emitted must be false everywhere, occurred_at must be a
//      well-formed timestamp)
//   3. public-safety recursive scan reused from agent-run-report-validation.mjs
//      (secret-like values / local-path leakage / forbidden keys)
//
// This is a single-file checker (Allowed Paths for #1405 does not include a
// scripts/lib/ helper split for this checker).
//
// Usage:
//   node scripts/check-agent-operation-session-index.mjs <file-or-glob ...>
//   pnpm run agent-operation-session-index:check

import { createHash } from 'node:crypto'
import { existsSync, readFileSync } from 'node:fs'
import { glob as fsGlob, stat } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { scanPublicSafety } from './lib/agent-run-report-validation.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
export const REPO_ROOT = resolve(__dirname, '..')

let Ajv2020, ajvFormats
try {
  const ajv2020Module = await import('ajv/dist/2020.js')
  Ajv2020 = ajv2020Module.default
  const formatsModule = await import('ajv-formats')
  ajvFormats = formatsModule.default
} catch (err) {
  console.error('Error: ajv and ajv-formats must be installed as devDependencies')
  console.error(err instanceof Error ? err.message : String(err))
  process.exit(1)
}

const SCHEMA_FILE = resolve(REPO_ROOT, 'docs/schemas/agent-operation-session-index.schema.json')

const EVENT_KIND_MAPPING = {
  issue_comment: new Set(['github_comment']),
  issue_body_update: new Set(['github_issue']),
  issue_close: new Set(['github_issue']),
  pr_open: new Set(['github_pr']),
  pr_comment: new Set(['github_comment']),
  pr_review_submitted: new Set(['github_pr']),
  pr_review_comment_created: new Set(['github_comment']),
  pr_review_thread_resolved: new Set(['github_pr']),
  commit_push: new Set(['workflow_run', 'github_pr']),
  ci_retry: new Set(['workflow_run']),
  merge: new Set(['github_pr']),
}

const PR_REVIEW_OPERATION_KIND_TO_SOURCE_KIND = {
  pr_review_submitted: 'github_pull_request_review',
  pr_review_comment_created: 'github_pull_request_review_comment',
  pr_review_thread_resolved: 'github_pull_request_review_thread',
}

function hasDuplicateValues(values) {
  const normalized = values.map((value) => String(value))
  return new Set(normalized).size !== normalized.length
}

function isPrReviewOperationKind(kind) {
  return Object.hasOwn(PR_REVIEW_OPERATION_KIND_TO_SOURCE_KIND, kind)
}

function createAjv() {
  const ajv = new Ajv2020({ strict: true, allErrors: true })
  ajvFormats(ajv)
  return ajv
}

function loadSchema() {
  return JSON.parse(readFileSync(SCHEMA_FILE, 'utf-8'))
}

function classifySchemaError(error) {
  if (error.keyword === 'required') {
    return 'schema.required'
  }
  if (error.keyword === 'additionalProperties') {
    return 'schema.unevaluated_property'
  }
  return 'schema.invalid'
}

export function validateAgentOperationSessionIndexAgainstSchema(payload) {
  const schema = loadSchema()
  const ajv = createAjv()
  const validate = ajv.compile(schema)
  const valid = validate(payload)
  if (valid) {
    return { valid: true, errors: [] }
  }
  const errors = (validate.errors || []).map((error) => ({
    path: error.instancePath || 'root',
    code: classifySchemaError(error),
    message: error.message || 'schema validation failed',
  }))
  return { valid: false, errors }
}

function extractCommentUrlNumber(url, kindSegment) {
  if (typeof url !== 'string') {
    return null
  }
  const match = url.match(new RegExp(`^https://github\\.com/squne121/loop-protocol/${kindSegment}/([0-9]+)#issuecomment-[0-9]+$`))
  return match ? Number(match[1]) : null
}

function urlKindSegment(kind) {
  return kind === 'issue' ? 'issues' : 'pull'
}

// semantic_checks (docs/schemas/agent-operation-session-index.schema.json companion
// checks — Issue #1405 AC3, OWNER review indication 5).
export function validateAgentOperationSessionIndexSemantics(index) {
  const errors = []
  const target = index?.target
  const parentIssue = index?.parent_issue
  const publicArtifacts = index?.public_artifacts || {}
  const operation = index?.operation || {}
  const verification = index?.verification || {}

  if (target && typeof target.kind === 'string' && typeof target.number === 'number') {
    const expectedSegment = urlKindSegment(target.kind)
    const urlFields = [
      ['public_artifacts.run_report_comment_url', publicArtifacts.run_report_comment_url],
      ['public_artifacts.retro_index_comment_url', publicArtifacts.retro_index_comment_url],
      ['public_artifacts.chatgpt_marker_comment_url', publicArtifacts.chatgpt_marker_comment_url],
    ]
    for (const [path, url] of urlFields) {
      if (typeof url !== 'string') {
        continue
      }
      const issuesNumber = extractCommentUrlNumber(url, 'issues')
      const pullNumber = extractCommentUrlNumber(url, 'pull')
      const actualSegment = issuesNumber !== null ? 'issues' : pullNumber !== null ? 'pull' : null
      if (actualSegment === null) {
        errors.push({
          path,
          code: 'target.kind_mismatch',
          message: `${path} is not a recognizable github issue/pull comment URL`,
        })
        continue
      }
      const number = actualSegment === 'issues' ? issuesNumber : pullNumber
      const parentIssueMatch = actualSegment === 'issues' && number === parentIssue
      if (actualSegment !== expectedSegment && !parentIssueMatch) {
        errors.push({
          path,
          code: 'target.kind_mismatch',
          message: `${path} URL kind (${actualSegment}) does not match target.kind (${target.kind})`,
        })
        continue
      }
      if (number !== target.number && number !== parentIssue) {
        errors.push({
          path,
          code: 'target.number_mismatch',
          message: `${path} references #${number}, which matches neither target.number (#${target.number}) nor parent_issue (#${parentIssue})`,
        })
      }
    }
  }

  const operationKind = operation.kind
  const eventKind = operation?.github_event_ref?.kind
  if (operationKind && eventKind) {
    const allowedEventKinds = EVENT_KIND_MAPPING[operationKind]
    if (!allowedEventKinds || !allowedEventKinds.has(eventKind)) {
      errors.push({
        path: 'operation.github_event_ref.kind',
        code: 'event_mapping.invalid',
        message: `github_event_ref.kind "${eventKind}" is not a valid mapping for operation.kind "${operationKind}"`,
      })
    }
  }

  if (index?.agent_run?.raw_values_emitted !== false) {
    errors.push({
      path: 'agent_run.raw_values_emitted',
      code: 'raw_values_emitted.violation',
      message: 'agent_run.raw_values_emitted must be false',
    })
  }

  if (isPrReviewOperationKind(operationKind)) {
    const expectedSourceKind = PR_REVIEW_OPERATION_KIND_TO_SOURCE_KIND[operationKind]
    const source = operation.source
    const resolver = verification.operation_source_resolver

    if (target?.kind !== 'pull_request') {
      errors.push({
        path: 'target.kind',
        code: 'target.kind_mismatch',
        message: `${operationKind} requires target.kind "pull_request"`,
      })
    }

    if (!source || source.kind !== expectedSourceKind) {
      errors.push({
        path: 'operation.source.kind',
        code: 'source.kind_mismatch',
        message: `operation.kind "${operationKind}" requires operation.source.kind "${expectedSourceKind}"`,
      })
    }

    if (source?.pull_number !== target?.number) {
      errors.push({
        path: 'operation.source.pull_number',
        code: 'source.target_mismatch',
        message: `operation.source.pull_number (#${source?.pull_number}) must match target.number (#${target?.number})`,
      })
    }

    if (!resolver || resolver.status !== 'resolved') {
      errors.push({
        path: 'verification.operation_source_resolver.status',
        code: 'resolver.status_not_resolved',
        message: 'verification.operation_source_resolver.status must be "resolved" for PR review operations',
      })
    }

    const pagination = resolver?.pagination
    if (pagination && Object.entries(pagination).some(([, complete]) => complete !== true)) {
      errors.push({
        path: 'verification.operation_source_resolver.pagination',
        code: 'resolver.pagination_incomplete',
        message: 'PR review surface pagination must be complete for reviews, review comments, review threads, and thread comments',
      })
    }

    const sourceCatalog = resolver?.source_catalog
    if (sourceCatalog) {
      if (hasDuplicateValues(sourceCatalog.review_ids ?? [])) {
        errors.push({
          path: 'verification.operation_source_resolver.source_catalog.review_ids',
          code: 'resolver.duplicate_source_id',
          message: 'review_ids must be unique',
        })
      }
      if (hasDuplicateValues(sourceCatalog.review_comment_ids ?? [])) {
        errors.push({
          path: 'verification.operation_source_resolver.source_catalog.review_comment_ids',
          code: 'resolver.duplicate_source_id',
          message: 'review_comment_ids must be unique',
        })
      }
      if (hasDuplicateValues(sourceCatalog.review_thread_node_ids ?? [])) {
        errors.push({
          path: 'verification.operation_source_resolver.source_catalog.review_thread_node_ids',
          code: 'resolver.duplicate_source_id',
          message: 'review_thread_node_ids must be unique',
        })
      }
    }

    if (source?.kind === 'github_pull_request_review') {
      if (source.state === 'PENDING') {
        errors.push({
          path: 'operation.source.state',
          code: 'review.state_pending',
          message: 'pr_review_submitted cannot reference a PENDING review',
        })
      }
      if (source.commit_id !== resolver?.target_commit) {
        errors.push({
          path: 'operation.source.commit_id',
          code: 'source.commit_mismatch',
          message: 'operation.source.commit_id must match verification.operation_source_resolver.target_commit',
        })
      }
      if (!sourceCatalog?.review_ids?.includes(source.review_id)) {
        errors.push({
          path: 'operation.source.review_id',
          code: 'source.catalog_missing',
          message: 'review_id must exist in verification.operation_source_resolver.source_catalog.review_ids',
        })
      }
    }

    if (source?.kind === 'github_pull_request_review_comment') {
      if (source.commit_id !== resolver?.target_commit) {
        errors.push({
          path: 'operation.source.commit_id',
          code: 'source.commit_mismatch',
          message: 'operation.source.commit_id must match verification.operation_source_resolver.target_commit',
        })
      }
      if (!sourceCatalog?.review_ids?.includes(source.review_id)) {
        errors.push({
          path: 'operation.source.review_id',
          code: 'source.catalog_missing',
          message: 'review_id must exist in verification.operation_source_resolver.source_catalog.review_ids',
        })
      }
      if (!sourceCatalog?.review_comment_ids?.includes(source.comment_id)) {
        errors.push({
          path: 'operation.source.comment_id',
          code: 'source.catalog_missing',
          message: 'comment_id must exist in verification.operation_source_resolver.source_catalog.review_comment_ids',
        })
      }
    }

    if (source?.kind === 'github_pull_request_review_thread') {
      if (source.is_resolved !== true || source.origin?.is_resolved !== true) {
        errors.push({
          path: 'operation.source.is_resolved',
          code: 'review_thread.unresolved',
          message: 'pr_review_thread_resolved must reference a resolved review thread',
        })
      }
      if (!sourceCatalog?.review_thread_node_ids?.includes(source.thread_node_id)) {
        errors.push({
          path: 'operation.source.thread_node_id',
          code: 'source.catalog_missing',
          message: 'thread_node_id must exist in verification.operation_source_resolver.source_catalog.review_thread_node_ids',
        })
      }
    }
  }

  return { valid: errors.length === 0, errors }
}

export function validateAgentOperationSessionIndex(payload) {
  const schemaResult = validateAgentOperationSessionIndexAgainstSchema(payload)
  const semanticResult = validateAgentOperationSessionIndexSemantics(payload)
  const scanResult = scanPublicSafety(payload)
  const filteredScanErrors = scanResult.errors.filter((error) => {
    if (error.code !== 'secret.token_like_hex40') {
      return true
    }
    return ![
      'operation.source.commit_id',
      'verification.operation_source_resolver.target_commit',
    ].includes(error.path)
  })
  const errors = [
    ...schemaResult.errors,
    ...semanticResult.errors,
    ...filteredScanErrors,
  ]
  return { valid: errors.length === 0, errors }
}

function stableStringify(value) {
  if (Array.isArray(value)) {
    return `[${value.map((entry) => stableStringify(entry)).join(',')}]`
  }
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableStringify(value[key])}`).join(',')}}`
  }
  return JSON.stringify(value)
}

export function computeAgentOperationSessionIndexPayloadDigest(payload) {
  return `sha256:${createHash('sha256').update(stableStringify(payload), 'utf-8').digest('hex')}`
}

function printUsage() {
  console.error('Usage: check-agent-operation-session-index.mjs [file-or-glob ...]')
}

function getDefaultCheckPatterns() {
  return [
    'tests/fixtures/agent-operation-session-index/valid-*.json',
    'artifacts/agent-operation-session-index*.json',
  ]
}

async function expandPatterns(patterns) {
  const files = []
  for (const pattern of patterns) {
    const absPattern = resolve(REPO_ROOT, pattern)
    if (existsSync(absPattern)) {
      const stats = await stat(absPattern)
      if (stats.isFile()) {
        files.push(absPattern)
        continue
      }
    }
    try {
      const matches = await Array.fromAsync(fsGlob(absPattern))
      files.push(...matches.map((match) => resolve(match)))
    } catch {
      // fall through
    }
  }
  return [...new Set(files)].sort()
}

async function main() {
  const args = process.argv.slice(2)
  const explicitTargets = args.length > 0
  const patterns = explicitTargets ? args : getDefaultCheckPatterns()
  const files = await expandPatterns(patterns)

  if (files.length === 0) {
    if (explicitTargets || process.env.CI === 'true') {
      printUsage()
      console.error('agent-operation-session-index:check: no files found')
      process.exit(1)
    }
    console.log('agent-operation-session-index:check: no files found (default targets) - skipped')
    process.exit(0)
  }

  let failures = 0
  for (const file of files) {
    const shortPath = file.replace(`${REPO_ROOT}/`, '')
    let json
    try {
      json = JSON.parse(readFileSync(file, 'utf-8'))
    } catch (err) {
      failures += 1
      console.error(`FAIL ${shortPath}`)
      console.error(`  - path: file`)
      console.error(`    code: file.json_parse`)
      console.error(`    message: ${err instanceof Error ? err.message : String(err)}`)
      continue
    }

    const result = validateAgentOperationSessionIndex(json)
    if (result.valid) {
      console.log(`PASS ${shortPath}`)
      continue
    }
    failures += 1
    console.error(`FAIL ${shortPath}`)
    for (const error of result.errors) {
      console.error(`  - path: ${error.path}`)
      console.error(`    code: ${error.code}`)
      console.error(`    message: ${error.message}`)
    }
  }

  process.exit(failures === 0 ? 0 : 1)
}

const isMain = process.argv[1] && resolve(process.argv[1]) === resolve(fileURLToPath(import.meta.url))
if (isMain) {
  main().catch((err) => {
    console.error(err instanceof Error ? err.message : String(err))
    process.exit(1)
  })
}
