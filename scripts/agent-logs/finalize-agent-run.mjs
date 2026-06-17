#!/usr/bin/env node

import { parseArgs, ensureEnum } from './lib/args.mjs'
import { loadDraft } from './lib/draft.mjs'
import { loadCommandSummaries } from './lib/command-summary-input.mjs'
import { buildReport } from './lib/report-builder.mjs'
import { validateFinalReport } from './lib/validate-final-report.mjs'
import { writeJsonAtomic } from './lib/atomic-json.mjs'

async function main() {
  try {
    const args = parseArgs(process.argv.slice(2), {
      draft: { required: true },
      output: { required: true },
      'command-summary-file': { required: true, multiple: true },
      'public-surface-kind': { defaultValue: 'none' },
      'manifest-refs-file': {},
      'evidence-refs-file': {},
      'docs-read-refs-file': {},
      'token-usage-file': {},
      'authority-file': {},
      'transcript-ref': {},
    })

    ensureEnum(args['public-surface-kind'], ['github_issue_comment', 'github_pr_comment', 'none'], 'public-surface-kind')

    if (args['transcript-ref'] && typeof args['transcript-ref'] !== 'string') {
      throw new Error('invalid_transcript_ref')
    }

    const draft = await loadDraft(args.draft)
    const commandsSummary = await loadCommandSummaries(args['command-summary-file'])
    const report = await buildReport({
      draft,
      publicSurfaceKind: args['public-surface-kind'],
      commandsSummary,
      manifestRefsFile: args['manifest-refs-file'],
      evidenceRefsFile: args['evidence-refs-file'],
      docsReadRefsFile: args['docs-read-refs-file'],
      tokenUsageFile: args['token-usage-file'],
      authorityFile: args['authority-file'],
    })

    validateFinalReport(report)
    await writeJsonAtomic(args.output, report)
    process.stdout.write('agent-run:finalize: ok\n')
  } catch (error) {
    const message = error?.message && /^[a-z0-9_:-]+$/i.test(error.message)
      ? error.message
      : 'agent_run_finalize_failed'
    process.stderr.write(`${message}\n`)
    process.exit(error.exitCode || 1)
  }
}

main()
