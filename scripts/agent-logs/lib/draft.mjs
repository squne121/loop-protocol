import { readFile } from 'fs/promises'

import {
  assertEnum,
  assertIsoTimestamp,
  assertNonEmptyString,
  assertPositiveIntegerString,
  runtimeError,
} from './args.mjs'

export const DRAFT_SCHEMA = 'agent_run_draft/v1'

export function createDraft(input) {
  return {
    schema: DRAFT_SCHEMA,
    run_id: assertNonEmptyString(input.runId, 'draft.run_id', 'run_id must be a non-empty string', { maxLength: 120 }),
    target: {
      kind: assertEnum(input.targetKind, ['issue', 'pull_request'], 'draft.target.kind', 'target kind must be issue or pull_request'),
      id: assertPositiveIntegerString(input.targetId, 'draft.target.id', 'target id must be a positive integer'),
    },
    phase: assertNonEmptyString(input.phase, 'draft.phase', 'phase must be a non-empty string', { maxLength: 80 }),
    actor: {
      type: assertEnum(input.actorType, ['ai_agent', 'github_action', 'human'], 'draft.actor.type', 'actor type is invalid'),
      name: assertNonEmptyString(input.actorName, 'draft.actor.name', 'actor name must be a non-empty string', { maxLength: 120 }),
    },
    started_at: assertIsoTimestamp(input.startedAt, 'draft.started_at', 'started_at must be an ISO-8601 timestamp'),
  }
}

export async function loadDraft(draftPath) {
  let parsed
  try {
    const raw = await readFile(draftPath, 'utf-8')
    parsed = JSON.parse(raw)
  } catch {
    throw runtimeError('draft.read_failed', 'draft file could not be read as JSON')
  }

  if (!parsed || typeof parsed !== 'object' || parsed.schema !== DRAFT_SCHEMA) {
    throw runtimeError('draft.invalid', 'draft payload is invalid')
  }

  const topLevelKeys = Object.keys(parsed).sort()
  const expectedTopLevelKeys = ['actor', 'phase', 'run_id', 'schema', 'started_at', 'target']
  if (JSON.stringify(topLevelKeys) !== JSON.stringify(expectedTopLevelKeys)) {
    throw runtimeError('draft.invalid', 'draft payload is invalid')
  }

  if (!parsed.target || typeof parsed.target !== 'object' || Array.isArray(parsed.target)) {
    throw runtimeError('draft.invalid', 'draft payload is invalid')
  }
  const targetKeys = Object.keys(parsed.target).sort()
  if (JSON.stringify(targetKeys) !== JSON.stringify(['id', 'kind'])) {
    throw runtimeError('draft.invalid', 'draft payload is invalid')
  }

  if (!parsed.actor || typeof parsed.actor !== 'object' || Array.isArray(parsed.actor)) {
    throw runtimeError('draft.invalid', 'draft payload is invalid')
  }
  const actorKeys = Object.keys(parsed.actor).sort()
  if (JSON.stringify(actorKeys) !== JSON.stringify(['name', 'type'])) {
    throw runtimeError('draft.invalid', 'draft payload is invalid')
  }

  return createDraft({
    runId: parsed.run_id,
    targetKind: parsed.target?.kind,
    targetId: String(parsed.target.id ?? ''),
    phase: parsed.phase,
    actorType: parsed.actor.type,
    actorName: parsed.actor.name,
    startedAt: parsed.started_at,
  })
}
