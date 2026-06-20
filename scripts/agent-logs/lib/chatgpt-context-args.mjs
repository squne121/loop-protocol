import { parseArgs, usageError } from './args.mjs'

export const OPTION_SPEC = {
  '--parent-issue-json': { key: 'parentIssueJson', required: true },
  '--target-issue-json': { key: 'targetIssueJson', required: true },
  '--retro-index-json': { key: 'retroIndexJson', required: true },
  '--source-set-json': { key: 'sourceSetJson', required: true },
  '--run-report-json': { key: 'runReportJson', multiple: true },
  '--evidence-ref-json': { key: 'evidenceRefJson', multiple: true },
  '--max-chars': { key: 'maxChars', required: true },
  '--max-sections': { key: 'maxSections', required: true },
  '--generated-at': { key: 'generatedAt', required: true },
  '--output': { key: 'outputPath', required: true },
  '--summary-json-out': { key: 'summaryJsonOut', required: true },
}

export function parseChatgptContextArgs(argv) {
  const options = parseArgs(argv, OPTION_SPEC)

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
    maxChars,
    maxSections,
    generatedAt: options.generatedAt,
  }
}
