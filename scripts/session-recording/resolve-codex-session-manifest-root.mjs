#!/usr/bin/env node
// Issue #1546: canonical external per-user state root resolver for Codex
// Stop/SubagentStop session manifests.
//
// Two selection paths:
//
//   1. CODEX_HOOK_MANIFEST_ROOT override (test isolation only; production
//      leaves it unset). The override value is used verbatim as the write
//      root, but is validated fail-before-mutation: it must be absolute,
//      outside the repository tree, owned by the current uid (checked on
//      the nearest existing ancestor when the override path itself does not
//      yet exist), and private (mode 0700) when it already exists. None of
//      these checks ever create a directory or file.
//
//   2. Canonical external per-user state root (production default, env
//      unset): base = XDG_STATE_HOME when absolute, else
//      "$HOME/.local/state" (a relative XDG_STATE_HOME is ignored and the
//      default is used instead, per the XDG Base Directory spec's guidance
//      to treat invalid values as unset). The canonical root is
//      "<base>/loop-protocol/session-manifests/v1/<repo_key>/codex", where
//      repo_key is the first 32 hex characters of
//      sha256(canonical_repository_url + NUL + realpath(repoRoot)).
//
// This module performs read-only filesystem introspection (existsSync /
// lstatSync / statSync / realpathSync) for validation purposes only. It
// never calls mkdir/write/chmod -- callers (write-codex-session-manifest.mjs)
// own all mutation.
//
// Issue #1546 OWNER Blocker 3: "repo outside" containment is decided on the
// *realpath* of the candidate root (and of its nearest existing ancestor
// when the root itself does not exist yet), never on a lexical/resolve()
// string comparison alone. A pre-existing static symlink anywhere along the
// path (e.g. XDG_STATE_HOME itself symlinked into the repository tree) is
// therefore rejected fail-closed, even though the lexical path looks like
// it is outside the repo. The chosen contract is strict: ANY symlink on an
// existing path component that participates in root selection is rejected
// outright (no "allowed symlink" carve-out), because there is no current
// production need to support a symlinked state root and a narrower
// allow-list would be strictly more code to audit for the same risk.

import { existsSync, lstatSync, realpathSync, statSync } from 'node:fs'
import { homedir } from 'node:os'
import { isAbsolute, join, relative, resolve, sep } from 'node:path'
import { createHash } from 'node:crypto'

export const CANONICAL_REPO_URL = 'https://github.com/squne121/loop-protocol'
export const MANIFEST_NAMESPACE_SEGMENTS = ['loop-protocol', 'session-manifests', 'v1']

export class ManifestRootOverrideRejectedError extends Error {
  constructor(reasonCode, detail) {
    super(`session manifest root override rejected: ${reasonCode}${detail ? ` (${detail})` : ''}`)
    this.name = 'ManifestRootOverrideRejectedError'
    this.reasonCode = reasonCode
  }
}

/**
 * first 32 hex chars of sha256(CANONICAL_REPO_URL + NUL + realpath(repoRoot)).
 *
 * The NUL separator is appended via a distinct hash.update('\0') call
 * rather than embedded inside a template literal, so the source file
 * itself stays plain ASCII/UTF-8 text (Issue #1546 OWNER Blocker 5: a
 * literal NUL byte in the source made GitHub's diff viewer treat this
 * security-sensitive resolver as a binary file, hiding it from normal
 * code review).
 */
export function computeRepoKey(repoRoot) {
  const realRoot = realpathSync(repoRoot)
  const hash = createHash('sha256')
  hash.update(CANONICAL_REPO_URL)
  hash.update('\0')
  hash.update(realRoot)
  return hash.digest('hex').slice(0, 32)
}

function nearestExistingAncestor(candidatePath) {
  let current = resolve(candidatePath)
  for (;;) {
    if (existsSync(current)) {
      return current
    }
    const parent = resolve(current, '..')
    if (parent === current) {
      return current
    }
    current = parent
  }
}

function isInsideRepo(realCandidate, repoRootReal) {
  return realCandidate === repoRootReal || realCandidate.startsWith(`${repoRootReal}${sep}`)
}

/**
 * Fail-closed symlink + repo-containment guard shared by the override path
 * and the canonical external state-root path (Issue #1546 OWNER Blocker 3).
 *
 * Walks up from `candidatePath` to the nearest existing ancestor and:
 *   1. rejects if that ancestor is itself a symlink (`lstatSync`), and
 *   2. rejects if the ancestor's *realpath* resolves inside the repository.
 *
 * `realpathSync` fully resolves every symlink component in the chain up to
 * the ancestor, so step 2 also catches a symlink at any level above the
 * ancestor that ultimately resolves inside the repo tree -- not just a
 * symlink at the ancestor itself.
 *
 * Returns the ancestor's realpath (useful for callers that also want to
 * re-anchor further checks against a fully resolved path).
 */
function assertNoSymlinkEscapeToRepo({ candidatePath, repoRootReal, reasonPrefix }) {
  const ancestor = nearestExistingAncestor(candidatePath)
  const ancestorLstat = lstatSync(ancestor)
  if (ancestorLstat.isSymbolicLink()) {
    throw new ManifestRootOverrideRejectedError(`${reasonPrefix}_ancestor_is_symlink`, ancestor)
  }
  const ancestorReal = realpathSync(ancestor)
  if (isInsideRepo(ancestorReal, repoRootReal)) {
    throw new ManifestRootOverrideRejectedError(`${reasonPrefix}_resolves_inside_repo`, ancestor)
  }
  return { ancestor, ancestorReal, ancestorLstat }
}

/**
 * Validate a CODEX_HOOK_MANIFEST_ROOT-style explicit root override
 * fail-before-mutation. Never creates anything. Throws
 * ManifestRootOverrideRejectedError on any violation; returns the
 * normalized absolute override path on success.
 */
export function validateManifestRootOverride({ overrideRoot, repoRoot }) {
  if (!isAbsolute(overrideRoot)) {
    throw new ManifestRootOverrideRejectedError('override_not_absolute', overrideRoot)
  }
  const repoRootReal = realpathSync(repoRoot)
  const normalizedOverride = resolve(overrideRoot)
  if (isInsideRepo(normalizedOverride, repoRootReal)) {
    throw new ManifestRootOverrideRejectedError('override_inside_repo', overrideRoot)
  }
  const { ancestor, ancestorReal, ancestorLstat } = assertNoSymlinkEscapeToRepo({
    candidatePath: normalizedOverride,
    repoRootReal,
    reasonPrefix: 'override',
  })
  if (typeof process.getuid === 'function' && ancestorLstat.uid !== process.getuid()) {
    throw new ManifestRootOverrideRejectedError('override_owner_mismatch', overrideRoot)
  }
  // The ancestor may equal the (existing) override path itself, or a
  // shallower parent when the override path does not exist yet. When the
  // override path exists, re-check its own type/mode directly (not just
  // the ancestor's), since a non-symlink ancestor does not guarantee the
  // exact target itself is a plain private directory.
  if (existsSync(normalizedOverride)) {
    const targetLstat = ancestor === normalizedOverride ? ancestorLstat : lstatSync(normalizedOverride)
    if (targetLstat.isSymbolicLink()) {
      throw new ManifestRootOverrideRejectedError('override_is_symlink', overrideRoot)
    }
    if (!targetLstat.isDirectory()) {
      throw new ManifestRootOverrideRejectedError('override_not_a_directory', overrideRoot)
    }
    const targetReal = ancestor === normalizedOverride ? ancestorReal : realpathSync(normalizedOverride)
    if (isInsideRepo(targetReal, repoRootReal)) {
      throw new ManifestRootOverrideRejectedError('override_resolves_inside_repo', overrideRoot)
    }
    const targetStat = statSync(normalizedOverride)
    if ((targetStat.mode & 0o777) !== 0o700) {
      throw new ManifestRootOverrideRejectedError('override_not_private', overrideRoot)
    }
  }
  return normalizedOverride
}

/**
 * Resolve the canonical external per-user state root (production default
 * path, no CODEX_HOOK_MANIFEST_ROOT override). A relative XDG_STATE_HOME is
 * ignored and the spec default ("$HOME/.local/state") is used instead.
 *
 * Issue #1546 OWNER Blocker 3: XDG_STATE_HOME's own realpath (not just the
 * lexical/resolve() string) is what determines repo containment, and any
 * existing symlink between XDG_STATE_HOME and the deepest constructible
 * path component is rejected fail-closed via assertNoSymlinkEscapeToRepo.
 */
export function resolveCanonicalExternalStateRoot({ env = process.env, repoRoot } = {}) {
  const rawStateHome = env.XDG_STATE_HOME
  const stateHomeBase = rawStateHome && isAbsolute(rawStateHome)
    ? resolve(rawStateHome)
    : join(homedir(), '.local', 'state')
  const repoKey = computeRepoKey(repoRoot)
  const root = join(stateHomeBase, ...MANIFEST_NAMESPACE_SEGMENTS, repoKey, 'codex')
  const repoRootReal = realpathSync(repoRoot)

  // Reject a symlinked XDG_STATE_HOME (or any symlinked component on the
  // way down to `root`) before any lexical containment check runs, so a
  // static pre-existing symlink cannot make a repo-internal write target
  // look "outside the repo" by lexical string comparison alone.
  assertNoSymlinkEscapeToRepo({ candidatePath: root, repoRootReal, reasonPrefix: 'state_home' })

  if (isInsideRepo(root, repoRootReal)) {
    // Structurally should never happen (state home vs. repo tree are
    // disjoint by construction), but keep the invariant explicit rather
    // than silently writing inside the repository.
    throw new ManifestRootOverrideRejectedError('resolved_root_inside_repo', root)
  }
  return { root, stateHome: stateHomeBase, repoKey }
}

/**
 * Resolve the canonical session manifest write root: a validated
 * CODEX_HOOK_MANIFEST_ROOT override when set, otherwise the canonical
 * external per-user state root. Returns
 * { root, source: 'override' | 'external_state_root', stateHome, repoKey }.
 * `stateHome` is null for the override source (an override-sourced
 * evidence_ref keeps the existing manifestRoot-relative "<event>/<file>"
 * form for back-compat with pre-#1546 test isolation callers).
 */
export function resolveCodexSessionManifestRoot({ env = process.env, repoRoot }) {
  const overrideRoot = env.CODEX_HOOK_MANIFEST_ROOT
  if (overrideRoot) {
    const validated = validateManifestRootOverride({ overrideRoot, repoRoot })
    return { root: validated, source: 'override', stateHome: null, repoKey: null }
  }
  const { root, stateHome, repoKey } = resolveCanonicalExternalStateRoot({ env, repoRoot })
  return { root, source: 'external_state_root', stateHome, repoKey }
}

/**
 * Re-verify, immediately before/after directory creation, that a resolved
 * write root's realpath is still outside the repository (Issue #1546 OWNER
 * Blocker 3: TOCTOU guard between resolution-time and mkdir-time). Exported
 * for write-codex-session-manifest.mjs to call after mkdirSync, before any
 * manifest content is written under the directory.
 */
export function assertRootRealpathOutsideRepo({ dirPath, repoRoot }) {
  const repoRootReal = realpathSync(repoRoot)
  const dirLstat = lstatSync(dirPath)
  if (dirLstat.isSymbolicLink()) {
    throw new ManifestRootOverrideRejectedError('manifest_directory_is_symlink', dirPath)
  }
  if (!dirLstat.isDirectory()) {
    throw new ManifestRootOverrideRejectedError('manifest_directory_not_a_directory', dirPath)
  }
  const dirReal = realpathSync(dirPath)
  if (isInsideRepo(dirReal, repoRootReal)) {
    throw new ManifestRootOverrideRejectedError('manifest_directory_resolves_inside_repo', dirPath)
  }
  return { dirLstat, dirReal, repoRootReal }
}

/**
 * Canonical evidence locator for an external_state_root-sourced final
 * manifest path: a state-root-relative POSIX-style path (never a raw
 * absolute home/repo path, never containing `..`).
 *
 * `stateHome` is realpath-resolved by the caller before this is invoked
 * (Issue #1546 OWNER Blocker 3), so the locator is anchored to the same
 * physical location as the final manifest file even when XDG_STATE_HOME
 * itself was reached through symlinked intermediate components that were
 * validated (not rejected) elsewhere in the chain.
 */
export function resolveCodexSessionManifestEvidenceRef({ stateHome, finalAbsolutePath }) {
  const rel = relative(stateHome, finalAbsolutePath)
  if (!rel || rel.startsWith('..') || isAbsolute(rel)) {
    throw new Error(
      `resolveCodexSessionManifestEvidenceRef: cannot form a safe locator for ${finalAbsolutePath}`
    )
  }
  return rel.split(sep).join('/')
}
