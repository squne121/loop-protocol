#!/usr/bin/env node

import { execFileSync } from 'node:child_process'
import { existsSync, readdirSync, realpathSync, statSync } from 'node:fs'
import { relative, resolve, sep } from 'node:path'

const FORBIDDEN_PREFIXES = ['assets', 'LICENSES']
const PUBLIC_ARTIFACT_ROOTS = ['artifacts']
const PUBLIC_ARTIFACT_NAME_PATTERNS = [/public/i, /preview/i, /checkpoint/i]
const REF_RISK_PATTERNS = [/checkpoint/i, /public/i]
const CONFIG_RISK_PATTERNS = [/^remote\..*\.pushurl\s+/i, /^branch\..*\.pushremote\s+/i, /checkpoint/i, /public/i]

function normalizeNewlines(text) {
  return String(text).replace(/\r\n/g, '\n')
}

function runGit(repoRoot, args) {
  return normalizeNewlines(
    execFileSync('git', args, {
      cwd: repoRoot,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe'],
    })
  )
}

function safeRealpath(path) {
  try {
    return realpathSync.native ? realpathSync.native(path) : realpathSync(path)
  } catch {
    return resolve(path)
  }
}

function toRepoRelative(repoRoot, candidatePath) {
  const absolute = safeRealpath(resolve(repoRoot, candidatePath))
  const relativePath = relative(repoRoot, absolute).split(sep).join('/')
  if (relativePath === '' || relativePath === '.') {
    return '.'
  }
  if (relativePath.startsWith('..')) {
    return `../${relativePath.slice(3)}`
  }
  return relativePath
}

function isForbiddenRelativePath(relativePath) {
  if (relativePath.startsWith('../')) {
    return true
  }
  const normalized = relativePath.replace(/^\.\//, '')
  if (normalized === '.env' || normalized.startsWith('.env.')) {
    return true
  }
  return normalized.split('/').some((segment, index) => {
    if (segment === '.env' || segment.startsWith('.env.')) {
      return true
    }
    return index === 0 && FORBIDDEN_PREFIXES.includes(segment)
  })
}

function isPublicArtifactRelativePath(relativePath) {
  if (relativePath.startsWith('../')) {
    return false
  }
  const normalized = relativePath.replace(/^\.\//, '')
  return PUBLIC_ARTIFACT_ROOTS.some((root) =>
    (normalized === root || normalized.startsWith(`${root}/`)) &&
    PUBLIC_ARTIFACT_NAME_PATTERNS.some((pattern) => pattern.test(normalized))
  )
}

function parseStatusEntries(repoRoot) {
  const output = runGit(repoRoot, ['status', '--porcelain=v1', '-z', '--untracked-files=all'])
  const entries = output.split('\0').filter(Boolean)
  const touched = []
  for (let index = 0; index < entries.length; index += 1) {
    const entry = entries[index]
    const statusCode = entry.slice(0, 2)
    if (statusCode[0] === 'R' || statusCode[0] === 'C') {
      const fromPath = entry.slice(3)
      const toPath = entries[index + 1] ?? ''
      touched.push(toRepoRelative(repoRoot, fromPath))
      if (toPath) {
        touched.push(toRepoRelative(repoRoot, toPath))
        index += 1
      }
      continue
    }
    touched.push(toRepoRelative(repoRoot, entry.slice(3)))
  }
  return [...new Set(touched)].sort()
}

function walkRelativeFiles(repoRoot, relativeRoot) {
  const absoluteRoot = resolve(repoRoot, relativeRoot)
  if (!existsSync(absoluteRoot)) {
    return []
  }
  const results = []
  const stack = [absoluteRoot]
  while (stack.length > 0) {
    const current = stack.pop()
    const currentStat = statSync(current)
    if (currentStat.isDirectory()) {
      for (const entry of readdirSync(current)) {
        stack.push(resolve(current, entry))
      }
      continue
    }
    results.push(toRepoRelative(repoRoot, current))
  }
  return results.sort()
}

function scanRiskyRefs(repoRoot) {
  const refs = runGit(repoRoot, ['for-each-ref', '--format=%(refname)']).split('\n').filter(Boolean)
  return refs.filter((ref) => REF_RISK_PATTERNS.some((pattern) => pattern.test(ref)))
}

function scanRiskyConfig(repoRoot) {
  try {
    return runGit(repoRoot, ['config', '--get-regexp', '.'])
      .split('\n')
      .filter(Boolean)
      .filter((line) => CONFIG_RISK_PATTERNS.some((pattern) => pattern.test(line)))
  } catch {
    return []
  }
}

export function verifyCodexPostRun(payload, options = {}) {
  const repoRoot = safeRealpath(options.repoRoot ?? process.cwd())
  const failures = []
  const touchedPaths = parseStatusEntries(repoRoot)
  const forbiddenTouched = touchedPaths.filter((path) => isForbiddenRelativePath(path))
  const publicArtifacts = walkRelativeFiles(repoRoot, 'artifacts').filter((path) => isPublicArtifactRelativePath(path))
  const riskyRefs = scanRiskyRefs(repoRoot)
  const riskyConfig = scanRiskyConfig(repoRoot)

  if (forbiddenTouched.length > 0) {
    failures.push(...forbiddenTouched.map((path) => `forbidden_path:${path}`))
  }
  if (publicArtifacts.length > 0) {
    failures.push(...publicArtifacts.map((path) => `public_artifact:${path}`))
  }
  if (riskyRefs.length > 0) {
    failures.push(...riskyRefs.map((ref) => `ref_risk:${ref}`))
  }
  if (riskyConfig.length > 0) {
    failures.push(...riskyConfig.map((line) => `config_risk:${line.split(/\s+/, 2)[0]}`))
  }

  const supplementalTouched = Array.isArray(payload?.touched_paths) ? payload.touched_paths : []
  for (const touchedPath of supplementalTouched) {
    const relativePath = toRepoRelative(repoRoot, touchedPath)
    if (isForbiddenRelativePath(relativePath) && !failures.includes(`forbidden_path:${relativePath}`)) {
      failures.push(`forbidden_path:${relativePath}`)
    }
    if (isPublicArtifactRelativePath(relativePath) && !failures.includes(`public_artifact:${relativePath}`)) {
      failures.push(`public_artifact:${relativePath}`)
    }
  }
  if (payload?.git_push_attempted === true) {
    failures.push('payload_git_push_attempted')
  }
  if (payload?.public_artifact_path === true) {
    failures.push('payload_public_artifact_path')
  }
  if (payload?.forbidden_path_touched === true) {
    failures.push('payload_forbidden_path_touched')
  }

  return {
    ok: failures.length === 0,
    failures: [...new Set(failures)],
  }
}
