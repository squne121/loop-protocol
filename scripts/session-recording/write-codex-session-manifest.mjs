#!/usr/bin/env node

import {
  chmodSync,
  closeSync,
  existsSync,
  fsyncSync,
  linkSync,
  lstatSync,
  mkdirSync,
  openSync,
  statSync,
  unlinkSync,
  writeSync,
} from 'node:fs'
import { join } from 'node:path'
import { randomUUID } from 'node:crypto'
import { Buffer } from 'node:buffer'

import {
  assertRootRealpathOutsideRepo,
  resolveCodexSessionManifestEvidenceRef,
  resolveCodexSessionManifestRoot,
} from './resolve-codex-session-manifest-root.mjs'

// Issue #1546 OWNER Blocker 2: every mutating fs primitive the writer calls
// is threaded through this object so tests can inject a fault (throw, or a
// short/partial return value) at any single step (write / fsync / link /
// unlink / close / directory fsync) and assert both (a) the original error
// still propagates undisguised and (b) no `.tmp` residue is left behind.
// Production callers never pass `fsOps` -- the defaults are the real
// node:fs primitives.
export const defaultFsOps = {
  chmodSync,
  closeSync,
  existsSync,
  fsyncSync,
  linkSync,
  lstatSync,
  mkdirSync,
  openSync,
  statSync,
  unlinkSync,
  writeSync,
}

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

/**
 * Create the manifest event directory (mode 0700), then re-verify it
 * fail-closed (Issue #1546 OWNER Blocker 3: TOCTOU guard between
 * resolution-time and mkdir-time, plus Blocker 3's "don't swallow chmod
 * failures / don't skip re-verifying a pre-existing directory" asks):
 *
 *   1. mkdirSync(recursive, mode 0700) -- a no-op if the directory (or an
 *      ancestor) already exists with the right type.
 *   2. assertRootRealpathOutsideRepo -- confirms the directory that now
 *      exists on disk is a real (non-symlink) directory whose realpath is
 *      still outside the repository, closing the window between resolving
 *      the root and creating it.
 *   3. chmodSync(0700) -- failures propagate (never swallowed): a chmod
 *      failure here means we cannot assert the directory is private, which
 *      must fail closed rather than silently continuing.
 *   4. statSync re-check -- confirms the directory is actually private
 *      (mode 0700) after the chmod call returned, rather than trusting the
 *      chmod call's mere absence-of-exception.
 */
function ensurePrivateDirectory(dirPath, repoRoot, fsOps) {
  fsOps.mkdirSync(dirPath, { recursive: true, mode: 0o700 })
  assertRootRealpathOutsideRepo({ dirPath, repoRoot })
  fsOps.chmodSync(dirPath, 0o700)
  const postChmod = fsOps.statSync(dirPath)
  if ((postChmod.mode & 0o777) !== 0o700) {
    throw new Error(`ensurePrivateDirectory: ${dirPath} is not private (mode 0700) after chmod`)
  }
}

/** Write `buffer` to `fd` in a loop until every byte has been written.
 *
 * Issue #1546 OWNER Blocker 2: fs.writeSync()'s return value is the number
 * of bytes actually written for that call, which can be less than the
 * requested length (short write) -- Node's own docs call this out
 * explicitly. A single unchecked writeSync() call can therefore silently
 * publish a truncated manifest. This loop keeps calling writeSync() with
 * the remaining slice until the whole buffer has been written.
 */
function writeFullSync(fsOps, fd, buffer) {
  let offset = 0
  while (offset < buffer.length) {
    const written = fsOps.writeSync(fd, buffer, offset, buffer.length - offset)
    if (!written || written <= 0) {
      throw new Error(`writeFullSync: writeSync() made no progress at offset ${offset}/${buffer.length}`)
    }
    offset += written
  }
}

function removeTempIfPresent(fsOps, tempPath) {
  if (fsOps.existsSync(tempPath)) {
    fsOps.unlinkSync(tempPath)
  }
}

/**
 * Create-once, no-overwrite publish protocol (Issue #1546 AC6, OWNER
 * Blocker 2/Blocker 5): exclusive-create a uniquely-named temp file with
 * mode 0600, write it fully (short-write-safe loop), fsync it, publish via
 * linkSync (which fails with EEXIST if the final path already exists,
 * unlike renameSync which would silently replace it), fsync the parent
 * directory (durability of the new directory entry, not just the file
 * content), and finally unlink the temp name.
 *
 * The entire temp-file lifecycle (open through final unlink) runs inside a
 * single try/finally: ANY failure at any step (write, fsync, link, unlink,
 * close) removes the temp file before the original error propagates
 * undisguised -- no `.tmp` residue survives any failure path, and a
 * cleanup-time secondary failure is logged (not thrown), so it never masks
 * the primary error that triggered cleanup.
 */
export function writeCodexSessionManifest({
  manifest,
  repoRoot,
  eventName,
  now = Date.now(),
  fileName = buildCodexManifestFileName(now),
  env = process.env,
  fsOps: fsOpsOverride = {},
}) {
  const fsOps = { ...defaultFsOps, ...fsOpsOverride }
  const target = resolveManifestWriteTarget({ repoRoot, eventName, fileName, env })
  ensurePrivateDirectory(target.directory, repoRoot, fsOps)

  const finalPath = target.absolutePath
  const tempName = `.${fileName}.${randomUUID()}.tmp`
  const tempPath = join(target.directory, tempName)
  const payload = Buffer.from(JSON.stringify(manifest, null, 2), 'utf8')

  let fd
  let tempCreated = false
  let primaryError

  try {
    fd = fsOps.openSync(tempPath, 'wx', 0o600)
    tempCreated = true
    writeFullSync(fsOps, fd, payload)
    fsOps.fsyncSync(fd)
    fsOps.closeSync(fd)
    fd = undefined

    try {
      fsOps.linkSync(tempPath, finalPath)
    } catch (err) {
      if (err && err.code === 'EEXIST') {
        throw new ManifestPublishConflictError(finalPath)
      }
      throw err
    }

    // Durability: fsync the parent directory so the new hard-link
    // directory entry itself (not just the linked file's content) is
    // persisted. fsync on the file descriptor above only guarantees the
    // *content* survived a crash, not that the directory entry pointing
    // at it did.
    let dirFd
    try {
      dirFd = fsOps.openSync(target.directory, 'r')
      fsOps.fsyncSync(dirFd)
    } finally {
      if (dirFd !== undefined) {
        fsOps.closeSync(dirFd)
      }
    }
  } catch (err) {
    primaryError = err
  } finally {
    if (fd !== undefined) {
      try {
        fsOps.closeSync(fd)
      } catch (closeErr) {
        // A close() failure while a primary error is already unwinding
        // never masks it (Issue #1546 OWNER Blocker 2: "cleanup失敗で元の
        // 例外を隠さない"). When there is no primary error yet, the close
        // failure itself becomes the reported error.
        primaryError = primaryError ?? closeErr
      }
    }
  }

  // Temp cleanup runs unconditionally on both the success path (the temp
  // name must never survive a successful publish) and every failure path
  // (open/write/fsync/link/dir-fsync/close). A cleanup-time failure here
  // is reported only when there was no earlier primary error to preserve;
  // otherwise it is swallowed (not silently -- the primary error is what
  // actually propagates, matching the OWNER ask that cleanup failures
  // never hide the real failure).
  if (tempCreated) {
    try {
      removeTempIfPresent(fsOps, tempPath)
    } catch (cleanupErr) {
      primaryError = primaryError ?? cleanupErr
    }
  }

  if (primaryError) {
    throw primaryError
  }

  return {
    absolutePath: finalPath,
    relativePath: target.relativePath,
    directory: target.directory,
  }
}
