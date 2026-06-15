#!/usr/bin/env node

import { existsSync, readFileSync } from 'fs'
import { glob as fsGlob, stat } from 'fs/promises'
import { resolve, dirname, extname } from 'path'
import { fileURLToPath } from 'url'
import {
  REPO_ROOT,
  extractPayloadFromMarkdown,
  getDefaultCheckPatterns,
  loadJsonFile,
  validateAgentRetroIndex,
  validateAgentRunReport,
} from './lib/agent-run-report-validation.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))

function printUsage() {
  console.error('Usage: check-agent-run-reports.mjs [--require-target] [file-or-glob ...]')
}

function parseArgs(argv) {
  const options = {
    requireTarget: false,
    targets: [],
  }

  for (const arg of argv) {
    if (arg === '--require-target') {
      options.requireTarget = true
      continue
    }
    if (arg.startsWith('--')) {
      return {
        ok: false,
        exitCode: 2,
        message: `unknown option: ${arg}`,
      }
    }
    options.targets.push(arg)
  }

  return {
    ok: true,
    options,
  }
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

function validateFile(filePath) {
  const ext = extname(filePath).toLowerCase()
  const shortPath = filePath.replace(`${REPO_ROOT}/`, '')

  if (ext === '.json') {
    const json = loadJsonFile(filePath)
    const validator = json?.schema === 'agent_retro_index/v1' ? validateAgentRetroIndex : validateAgentRunReport
    const result = validator(json)
    return {
      file: shortPath,
      valid: result.valid,
      errors: result.errors,
    }
  }

  if (ext === '.md') {
    const markdown = readFileSync(filePath, 'utf-8')
    const extraction = extractPayloadFromMarkdown(markdown)
    if (!extraction.ok) {
      return {
        file: shortPath,
        valid: false,
        errors: [extraction.error],
      }
    }
    const result = extraction.schemaName === 'agent_retro_index/v1'
      ? validateAgentRetroIndex(extraction.payload)
      : validateAgentRunReport(extraction.payload)
    return {
      file: shortPath,
      valid: result.valid,
      errors: result.errors,
    }
  }

  return {
    file: shortPath,
    valid: false,
    errors: [{
      path: 'file',
      code: 'file.unsupported_type',
      message: `unsupported file type: ${ext || '<none>'}`,
    }],
  }
}

async function main() {
  const parsed = parseArgs(process.argv.slice(2))
  if (!parsed.ok) {
    printUsage()
    console.error(parsed.message)
    process.exit(parsed.exitCode)
  }

  const explicitTargets = parsed.options.targets.length > 0
  const patterns = explicitTargets ? parsed.options.targets : getDefaultCheckPatterns()
  const files = await expandPatterns(patterns)

  if (files.length === 0) {
    if (explicitTargets || parsed.options.requireTarget || process.env.CI === 'true') {
      console.error('agent-run-report:check: no files found')
      process.exit(1)
    }
    console.log('agent-run-report:check: no files found (default targets) - skipped')
    process.exit(0)
  }

  let failures = 0
  for (const file of files) {
    const result = validateFile(file)
    if (result.valid) {
      console.log(`PASS ${result.file}`)
      continue
    }

    failures += 1
    console.error(`FAIL ${result.file}`)
    for (const error of result.errors) {
      console.error(`  - path: ${error.path}`)
      console.error(`    code: ${error.code}`)
      console.error(`    message: ${error.message}`)
    }
  }

  process.exit(failures === 0 ? 0 : 1)
}

main().catch((err) => {
  console.error(err instanceof Error ? err.message : String(err))
  process.exit(1)
})
