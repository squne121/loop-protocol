#!/usr/bin/env node

import { mkdirSync, realpathSync, renameSync, writeFileSync, chmodSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, relative, resolve } from 'node:path'
import { randomUUID } from 'node:crypto'

export function buildCodexManifestFileName(now = Date.now()) {
  return `${now}-${randomUUID()}.json`
}

// Pure resolution helper (no I/O): computes the manifest write target given
// repoRoot/eventName/manifestRoot/fileName. Shared by writeCodexSessionManifest
// (which performs the actual I/O) and callers that need the resolved evidence_ref
// BEFORE the manifest is written (Issue #1420 fix_delta AC9/AC10).
export function resolveManifestWriteTarget({ repoRoot, eventName, manifestRoot, fileName }) {
  // AC1/AC2: when `manifestRoot` is provided, write under `<manifestRoot>/<eventName>/`.
  // When omitted (undefined/null/empty), fall back to the existing repoRoot-based
  // default path to preserve production hook behavior (back-compat).
  const directory = manifestRoot
    ? resolve(manifestRoot, eventName.toLowerCase())
    : resolve(repoRoot, 'tmp', 'session-manifests', 'codex', eventName.toLowerCase())
  const absolutePath = join(directory, fileName)
  // Issue #1420 fix_delta AC10: relativePath is embedded in the manifest's
  // evidence_ref and also passed to generate-session-manifest.mjs's
  // --evidence-source-ref (which fail-closes on absolute-looking / traversal
  // refs as a secret-leak guard). When manifestRoot is an override that lives
  // outside repoRoot (test isolation via CODEX_HOOK_MANIFEST_ROOT / tmp_path),
  // relative(repoRoot, absolutePath) would emit a `../../...` string that trips
  // that guard, so fall back to a manifestRoot-relative `<event>/<file>` form
  // in that case. The production default (manifestRoot unset) keeps the
  // existing repoRoot-relative `tmp/session-manifests/codex/<event>/<file>`
  // format unchanged.
  const relativePath = manifestRoot
    ? join(eventName.toLowerCase(), fileName)
    : relative(repoRoot, absolutePath)
  return {
    absolutePath,
    relativePath,
    directory,
  }
}

export function writeCodexSessionManifest({
  manifest,
  repoRoot,
  eventName,
  now = Date.now(),
  fileName = buildCodexManifestFileName(now),
  manifestRoot,
}) {
  const target = resolveManifestWriteTarget({ repoRoot, eventName, manifestRoot, fileName })
  const root = target.directory
  mkdirSync(root, { recursive: true })

  // AC9 (#1420 fix_delta): when an explicit manifestRoot override is supplied
  // (test isolation / CODEX_HOOK_MANIFEST_ROOT), fail closed unless the write
  // directory's realpath resolves under the OS tmpdir realpath. realpathSync is
  // called AFTER mkdirSync so a directory-replacing symlink swap is also caught,
  // not just a symlink pointing to an already-existing directory.
  if (manifestRoot) {
    const allowedBase = realpathSync(tmpdir())
    const resolvedRoot = realpathSync(root)
    const withinAllowedBase =
      resolvedRoot === allowedBase || resolvedRoot.startsWith(`${allowedBase}/`)
    if (!withinAllowedBase) {
      throw new Error(
        `writeCodexSessionManifest: manifestRoot resolved outside allowed base (${allowedBase}): ${resolvedRoot}`
      )
    }
  }

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
