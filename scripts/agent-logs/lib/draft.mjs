import { readFile } from 'fs/promises'

export async function loadDraft(filePath) {
  let content
  try {
    content = await readFile(filePath, 'utf-8')
  } catch {
    throw new Error('draft_read_failed')
  }

  let draft
  try {
    draft = JSON.parse(content)
  } catch {
    throw new Error('draft_parse_failed')
  }

  if (draft?.schema !== 'agent_run_draft/v1') {
    throw new Error('invalid_draft_schema')
  }
  return draft
}

export function createDraft({ runId, target, phase, actorName, actorType, startedAt }) {
  return {
    schema: 'agent_run_draft/v1',
    run_id: runId,
    target,
    phase,
    actor: {
      type: actorType,
      name: actorName,
    },
    started_at: startedAt,
  }
}
