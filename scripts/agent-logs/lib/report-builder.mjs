import { readFile } from 'fs/promises'

async function readJsonFile(filePath, fallbackValue) {
  if (!filePath) {
    return fallbackValue
  }
  let raw
  try {
    raw = await readFile(filePath, 'utf-8')
  } catch {
    throw new Error('structured_input_read_failed')
  }
  try {
    return JSON.parse(raw)
  } catch {
    throw new Error('structured_input_parse_failed')
  }
}

export async function buildReport({
  draft,
  publicSurfaceKind,
  commandsSummary,
  manifestRefsFile,
  evidenceRefsFile,
  docsReadRefsFile,
  tokenUsageFile,
  authorityFile,
}) {
  return {
    schema: 'agent_run_report/v1',
    public_surface_kind: publicSurfaceKind,
    public_safety: {
      redaction_status: 'clean',
      checked_by: 'pnpm agent-run:check',
      validator_version: '1.0.0',
      checked_at: new Date().toISOString(),
      verdict: 'pass',
      blocked_reasons: [],
    },
    actor: draft.actor,
    authority: await readJsonFile(authorityFile, {
      level: 'non_authoritative',
      basis: 'ai_self_report',
      evidence_refs: [],
    }),
    token_usage: await readJsonFile(tokenUsageFile, {
      availability: 'unavailable',
      source: 'none',
      prompt: null,
      completion: null,
      total: null,
    }),
    manifest_refs: await readJsonFile(manifestRefsFile, []),
    evidence_refs: await readJsonFile(evidenceRefsFile, []),
    commands_summary: commandsSummary,
    docs_read_refs: await readJsonFile(docsReadRefsFile, []),
  }
}
