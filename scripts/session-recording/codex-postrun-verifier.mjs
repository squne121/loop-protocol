#!/usr/bin/env node

const FORBIDDEN_PATH_PATTERNS = [
  /^assets\//,
  /^LICENSES\//,
  /^\.env$/,
  /^\.env\./,
]

export function verifyCodexPostRun(payload) {
  const failures = []
  const touchedPaths = Array.isArray(payload?.touched_paths) ? payload.touched_paths : []

  if (payload?.git_push_attempted === true) {
    failures.push('git_push_attempted')
  }
  if (payload?.public_artifact_path === true) {
    failures.push('public_artifact_path')
  }
  if (payload?.forbidden_path_touched === true) {
    failures.push('forbidden_path_touched')
  }

  for (const touchedPath of touchedPaths) {
    const normalized = String(touchedPath).replace(/^\.?\//, '')
    if (FORBIDDEN_PATH_PATTERNS.some((pattern) => pattern.test(normalized))) {
      failures.push(`forbidden_path:${normalized}`)
    }
  }

  return {
    ok: failures.length === 0,
    failures,
  }
}
