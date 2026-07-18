#!/usr/bin/env node

import { chmodSync, closeSync, existsSync, fsyncSync, linkSync, mkdirSync, openSync, unlinkSync, writeSync } from 'node:fs'
import { join } from 'node:path'
import { randomUUID } from 'node:crypto'

import {
  resolveCodexSessionManifestEvidenceRef,
  resolveCodexSessionManifestRoot,
} from './resolve-codex-session-manifest-root.mjs'

export function buildCodexManifestFileName(now = Date.now()) {
  return `${now}-${randomUUID()}.json`
}

export class ManifestPublishConflictError extends Error {
  constructor(finalPath) {
    super(`writeCodexSessionManifest: final manifest already exists, refusing to overwrite: ${finalPath}`)
    this.name = 'ManifestPublishConflictError'
    this.finalPath = finalPath
  }
}

// Resolution helper (validation-only I/O, never a mutation): computes the
// manifest write target given repoRoot/eventName/fileName/env. Delegates
// canonical root selection (CODEX_HOOK_MANIFEST_ROOT override vs. the
// canonical external per-user state root) to resolveCodexSessionManifestRoot,
// which validates any override fail-before-mutation but never itself
// creates a directory or file. Shared by writeCodexSessionManifest (which
// performs the actual I/O) and callers that need the resolved evidence_ref
// BEFORE the manifest is written (Issue #1420 fix_delta AC9/AC10; Issue
// #1546 externalization to a per-user state root).
export function resolveManifestWriteTarget({ repoRoot, eventName, fileName, env = process.env }) {
  const { root, source, stateHome } = resolveCodexSessionManifestRoot({ env, repoRoot })
  const directory = join(root, eventName.toLowerCase())
  const absolutePath = join(directory, fileName)
  // Issue #1546 Blocker 6: evidence_source_ref is a canonical
  // state-root-relative locator for the production (external_state_root)
  // path. An override-sourced root (test isolation via
  // CODEX_HOOK_MANIFEST_ROOT) keeps the existing manifestRoot-relative
  // "<event>/<file>" form for back-compat with pre-#1546 callers/tests --
  // that override root is a test-only construct with no canonical
  // state-home anchor to express the locator against.
  const relativePath = source === 'external_state_root'
    ? resolveCodexSessionManifestEvidenceRef({ stateHome, finalAbsolutePath: absolutePath })
    : join(eventName.toLowerCase(), fileName)
  return {
    absolutePath,
    relativePath,
    directory,
    source,
  }
}

function ensurePrivateDirectory(dirPath) {
  mkdirSync(dirPath, { recursive: true, mode: 0o700 })
  try {
    chmodSync(dirPath, 0o700)
  } catch {
    // Best effort only (mirrors the file-mode best-effort chmod below):
    // an already-0700 directory created by a concurrent writer is fine.
  }
}

/**
 * Create-once, no-overwrite publish protocol (Issue #1546 AC6, OWNER
 * Blocker 5): exclusive-create a uniquely-named temp file with mode 0600,
 * fsync it, then publish via linkSync (which fails with EEXIST if the final
 * path already exists, unlike renameSync which would silently replace it),
 * and finally unlink the temp name. An existing final manifest is never
 * overwritten, and no `.tmp` residue is left behind on any failure path.
 */
export function writeCodexSessionManifest({
  manifest,
  repoRoot,
  eventName,
  now = Date.now(),
  fileName = buildCodexManifestFileName(now),
  env = process.env,
}) {
  const target = resolveManifestWriteTarget({ repoRoot, eventName, fileName, env })
  ensurePrivateDirectory(target.directory)

  const finalPath = target.absolutePath
  const tempName = `.${fileName}.${randomUUID()}.tmp`
  const tempPath = join(target.directory, tempName)
  const payload = JSON.stringify(manifest, null, 2)

  let fd
  try {
    fd = openSync(tempPath, 'wx', 0o600)
    writeSync(fd, payload, 0, 'utf8')
    fsyncSync(fd)
  } finally {
    if (fd !== undefined) {
      closeSync(fd)
    }
  }

  try {
    linkSync(tempPath, finalPath)
  } catch (err) {
    if (existsSync(tempPath)) {
      unlinkSync(tempPath)
    }
    if (err && err.code === 'EEXIST') {
      throw new ManifestPublishConflictError(finalPath)
    }
    throw err
  }
  unlinkSync(tempPath)

  return {
    absolutePath: finalPath,
    relativePath: target.relativePath,
    directory: target.directory,
  }
}
