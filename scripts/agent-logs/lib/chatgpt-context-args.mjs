import { parseArgs, usageError } from './args.mjs'

export const OPTION_SPEC = {
  '--parent-issue-json': { key: 'parentIssueJson' },
  '--target-issue-json': { key: 'targetIssueJson' },
  '--retro-index-json': { key: 'retroIndexJson' },
  '--source-set-json': { key: 'sourceSetJson' },
  '--run-report-json': { key: 'runReportJson', multiple: true },
  '--evidence-ref-json': { key: 'evidenceRefJson', multiple: true },
  '--marker-comment-json': { key: 'markerCommentJson' },
  '--github-comments-json': { key: 'githubCommentsJson', multiple: true },
  '--max-chars': { key: 'maxChars', required: true },
  '--max-sections': { key: 'maxSections', required: true },
  '--generated-at': { key: 'generatedAt', required: true },
  '--output': { key: 'outputPath', required: true },
  '--summary-json-out': { key: 'summaryJsonOut', required: true },
}

export function parseChatgptContextArgs(argv) {
  const options = parseArgs(argv, OPTION_SPEC)
  const markerMode = typeof options.markerCommentJson === 'string'
  const fileMode = [
    options.parentIssueJson,
    options.targetIssueJson,
    options.retroIndexJson,
    options.sourceSetJson,
  ].every((value) => typeof value === 'string')

  if (!markerMode && !fileMode) {
    throw usageError(
      'cli.missing_source_mode',
      'provide either the legacy JSON source set or --marker-comment-json with --github-comments-json'
    )
  }
  if (markerMode && fileMode) {
    throw usageError(
      'cli.mixed_source_mode',
      'do not mix --marker-comment-json mode with legacy JSON source inputs'
    )
  }
  if (markerMode && (!Array.isArray(options.githubCommentsJson) || options.githubCommentsJson.length === 0)) {
    throw usageError(
      'cli.marker_comments_missing',
      '--marker-comment-json requires at least one --github-comments-json input'
    )
  }

  const maxChars = Number(options.maxChars)
  if (!Number.isInteger(maxChars) || maxChars < 1) {
    throw usageError('cli.invalid_max_chars', '--max-chars must be a positive integer')
  }

  const maxSections = Number(options.maxSections)
  if (!Number.isInteger(maxSections) || maxSections < 1) {
    throw usageError('cli.invalid_max_sections', '--max-sections must be a positive integer')
  }

  const generatedAt = new Date(options.generatedAt)
  if (Number.isNaN(generatedAt.getTime())) {
    throw usageError('cli.invalid_generated_at', '--generated-at must be a valid ISO-8601 timestamp')
  }

  return {
    ...options,
    sourceMode: markerMode ? 'marker_comment' : 'json_files',
    maxChars,
    maxSections,
    generatedAt: options.generatedAt,
  }
}
