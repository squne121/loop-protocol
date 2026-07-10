#!/usr/bin/env node

import { mkdirSync, renameSync, writeFileSync, chmodSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'
import { randomUUID } from 'node:crypto'

export function buildCodexManifestFileName(now = Date.now()) {
  return `${now}-${randomUUID()}.json`
}

export function writeCodexSessionManifest({
  manifest,
  repoRoot,
  eventName,
  now = Date.now(),
  fileName = buildCodexManifestFileName(now),
  manifestRoot,
}) {
  // AC1/AC2: when `manifestRoot` is provided, write under `<manifestRoot>/<eventName>/`.
  // When omitted (undefined/null/empty), fall back to the existing repoRoot-based
  // default path to preserve production hook behavior (back-compat).
  const root = manifestRoot
    ? resolve(manifestRoot, eventName.toLowerCase())
    : resolve(repoRoot, 'tmp', 'session-manifests', 'codex', eventName.toLowerCase())
  mkdirSync(root, { recursive: true })
  const finalPath = join(root, fileName)
  const tempPath = join(root, `${fileName}.tmp`)
  const payload = JSON.stringify(manifest, null, 2)

  writeFileSync(tempPath, payload, { encoding: 'utf8', mode: 0o600 })
  try {
    chmodSync(tempPath, 0o600)
  } catch {
    // Best effort only.
  }
  renameSync(tempPath, finalPath)

  return {
    absolutePath: finalPath,
    relativePath: relative(repoRoot, finalPath),
    directory: dirname(finalPath),
  }
}
