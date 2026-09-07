#!/usr/bin/env node
/**
 * Issue #1527 Scope Delta (2) AC17: real production readiness bootstrap.
 *
 * Unlike the prior no-op implementation (which wrote `prepared: true`
 * without ever provisioning anything), this script actually:
 *   1. locked-syncs the project's default uv dependency group (pyyaml,
 *      jsonschema — the capture producer's runtime dependencies),
 *   2. resolves the fixed interpreter realpath/version uv would use,
 *   3. runs an import smoke test for PyYAML and a py_compile smoke test
 *      for the capture producer script,
 *   4. binds the readiness artifact to repo root, uv.lock digest,
 *      .python-version digest, interpreter realpath/version, and the
 *      producer script's own digest,
 *   5. only then atomically writes the readiness artifact (mode 0600).
 *
 * Any failure at any step exits non-zero WITHOUT writing `prepared: true`
 * (or any artifact at all) — never a false-positive readiness claim.
 *
 * The hot path (scripts/session-recording/codex-hook-adapter.mjs) never
 * calls `uv run --locked` (which may sync); it spawns the fixed
 * interpreter_realpath recorded here directly, so cold-environment sync
 * never happens inline with a live SubagentStop hook (AC8).
 */

import { createHash } from 'node:crypto'
import { execFileSync } from 'node:child_process'
import {
  closeSync,
  constants as fsConstants,
  existsSync,
  fchmodSync,
  fstatSync,
  lstatSync,
  mkdirSync,
  openSync,
  readFileSync,
  renameSync,
  writeFileSync,
} from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
// scriptRepoRoot is where THIS script and its runtime dependencies actually
// live (uv.lock, .python-version, the capture producer script) -- it never
// changes, even under --repo-root below.
const scriptRepoRoot = resolve(__dirname, '..', '..')
const producerPath = resolve(scriptRepoRoot, '.claude', 'hooks', 'capture_scope_rollup_final_response.py')

// Issue #2004 P2: an optional `--repo-root <path>` test seam. Production
// callers never pass it (repoRoot === scriptRepoRoot). It exists so a
// hermetic isolated-fixture test can invoke the REAL bootstrap producer end
// to end while binding the readiness artifact's repo_root_realpath (and its
// own default artifact path) to an isolated tmp_path fixture repo, without
// mutating the real repo's canonical runtime artifact and without needing a
// second no-op reimplementation of this script's logic in the test suite.
// uv sync / interpreter resolution / digest inputs (uv.lock, .python-version,
// the producer script) always come from scriptRepoRoot -- a fixture repo
// generally has none of those.
function parseRepoRootArg(argv) {
  const idx = argv.indexOf('--repo-root')
  if (idx === -1) return null
  const value = argv[idx + 1]
  if (!value) {
    fail('--repo-root requires a value')
  }
  return resolve(value)
}

const repoRoot = parseRepoRootArg(process.argv.slice(2)) ?? scriptRepoRoot

class ArtifactOverridePathError extends Error {
  constructor(reasonCode) {
    super(reasonCode)
    this.reasonCode = reasonCode
  }
}

// Issue #2028: carries the original errno (`err.code` from the failed
// `mkdirSync`/`openSync` call, when available) and which of those two
// syscalls failed, all the way through to any caller that merely reads
// `.message` (the existing `main()` CLI failure path, and the
// `--test-invoke-prepare-private-parent-dir` test seam below, both already
// do only that) -- without requiring either of those call sites to change.
// The diagnostic reason code remains the FIRST token of `.message` so
// existing exact-match consumers can still match on a stable prefix.
//
// Declared here (near the top of the module, ahead of the argv-dispatched
// `--test-invoke-prepare-private-parent-dir` seam below) rather than next to
// `preparePrivateParentDir()` itself: that seam calls
// `preparePrivateParentDir()` synchronously at module top-level evaluation
// time, before any later `class` declaration in the file would have run.
//
// Issue #2028 fix_delta (PR #2549 review): the diagnostic message previously
// dropped the failing filesystem path entirely, so a CLI user could no
// longer see WHICH path triggered e.g. `parent_unavailable (errno=ELOOP,
// syscall=mkdirSync)` -- only the raw pre-fix uncaught exception's message
// used to show it. This now retains it as an additional `path="..."`
// segment, preferring the underlying fs error's own `.path` (surfaced via
// `cause.path`, since `cause` IS that original fs error) and falling back to
// the `path` option (the classifier's `dir` argument) when the fs error
// itself did not carry one. `reasonCode` stays the first token of
// `.message` and `errno`/`op` are unchanged -- only the path is added.
class PrivateParentDirError extends Error {
  constructor(reasonCode, { errno = null, op = null, cause, path = null } = {}) {
    let resolvedPath = null
    if (cause && typeof cause.path === 'string') {
      resolvedPath = cause.path
    } else if (typeof path === 'string') {
      resolvedPath = path
    }
    const parts = []
    if (errno || op) {
      parts.push(`errno=${errno ?? 'unknown'}`, `syscall=${op ?? 'unknown'}`)
    }
    if (resolvedPath !== null) {
      parts.push(`path=${JSON.stringify(resolvedPath)}`)
    }
    const detail = parts.length ? ` (${parts.join(', ')})` : ''
    super(`${reasonCode}${detail}`, cause === undefined ? undefined : { cause })
    this.reasonCode = reasonCode
    this.errnoCode = errno
    this.op = op
    this.path = resolvedPath
  }
}

// Issue #2004 P1-3: pure string-level (never filesystem-touching) lexical
// normalization, kept byte-for-byte in parity with the Python eligibility
// producer/consumer's _lexically_normalize_override_segments(). Node's
// path.resolve() never resolves symlinks (unlike Python's Path.resolve(),
// which the sibling Python producer used to call), but it DID silently
// collapse `..` segments for an absolute override while the Python side
// left an absolute override completely unnormalized -- the mirror-image
// divergence this fixes. `..` is now rejected outright (never collapsed)
// in both languages, and symlink acceptance/rejection is handled
// exclusively by preparePrivateParentDir() below -- never here.
function lexicallyNormalizeOverrideSegments(raw) {
  if (raw.indexOf('\u0000') !== -1) {
    throw new ArtifactOverridePathError('override_path_contains_nul')
  }
  const isAbsolute = raw.startsWith('/')
  const segments = []
  for (const part of raw.split('/')) {
    if (part === '' || part === '.') continue
    if (part === '..') {
      throw new ArtifactOverridePathError('override_path_contains_dotdot')
    }
    segments.push(part)
  }
  return { isAbsolute, segments }
}

// Issue #2004 AC4: an absolute override is used as-is; a relative override
// is resolved against repoRoot (never process.cwd()), matching the Python
// eligibility producer/consumer's resolve_session_recording_artifact_override().
function resolveReadinessOverride(overrideValue, repoRootPath) {
  if (!overrideValue) return null
  const { isAbsolute, segments } = lexicallyNormalizeOverrideSegments(overrideValue)
  return isAbsolute ? '/' + segments.join('/') : resolve(repoRootPath, ...segments)
}

// Issue #2004 P1-3: a lightweight diagnostic-only CLI seam
// (`--print-resolved-override <override> <repoRoot>`) so a cross-language
// parity test can invoke JUST resolveReadinessOverride() -- without paying
// for a full `uv sync` + interpreter-resolution bootstrap run -- and
// compare its output byte-for-byte against
// resolve_session_recording_artifact_override() on the Python side. Exits
// immediately; never reached by production callers.
function maybePrintResolvedOverrideAndExit() {
  const argv = process.argv.slice(2)
  const idx = argv.indexOf('--print-resolved-override')
  if (idx === -1) return
  const overrideValue = argv[idx + 1]
  const repoRootArg = argv[idx + 2]
  if (!overrideValue || !repoRootArg) {
    fail('--print-resolved-override requires <override> <repoRoot>')
    return
  }
  try {
    const resolved = resolveReadinessOverride(overrideValue, repoRootArg)
    process.stdout.write(`${resolved}\n`)
    process.exit(0)
  } catch (err) {
    process.stdout.write(`ERROR:${err?.reasonCode ?? err?.message ?? err}\n`)
    process.exit(3)
  }
}
maybePrintResolvedOverrideAndExit()

// Issue #2029 test seam: invoke preparePrivateParentDir() directly from an
// actual separate child process
// (`--test-invoke-prepare-private-parent-dir <dir>`) so an isolated-fixture
// regression test can assert its real fail-closed rejection of a
// mkfifo-created FIFO parent directory WITHOUT ever blocking, without
// duplicating preparePrivateParentDir()'s logic in the test suite itself.
// Exits 0 with `ACCEPTED` or `REJECTED:<reason>` on stdout in both the
// success and the handled-rejection case (only a genuine crash -- e.g. a
// missing arg, or an uncaught non-PrivateParentDirError bug -- exits
// non-zero); the parent test process is the one that enforces the external
// watchdog timeout via its own subprocess spawn call, never a same-thread
// timer inside this process. Never reached by production callers.
function maybeInvokePreparePrivateParentDirAndExit() {
  const argv = process.argv.slice(2)
  const idx = argv.indexOf('--test-invoke-prepare-private-parent-dir')
  if (idx === -1) return
  const dirArg = argv[idx + 1]
  if (!dirArg) {
    fail('--test-invoke-prepare-private-parent-dir requires <dir>')
    return
  }
  try {
    preparePrivateParentDir(dirArg)
    process.stdout.write('ACCEPTED\n')
    process.exit(0)
  } catch (err) {
    process.stdout.write(`REJECTED:${err?.message ?? err}\n`)
    process.exit(0)
  }
}
maybeInvokePreparePrivateParentDirAndExit()

// Issue #2004: moved from .claude/tmp/ to tmp/ (repo-approved local
// temporary workspace root, Issue #1995 / #2001) in lockstep with the
// eligibility producer/loader (check_session_recording_runtime_safety.py)
// and the readiness consumer (capture_scope_rollup_final_response.py). The
// old default location is never consulted as a fallback.
const readinessPath = resolveReadinessOverride(process.env.SCOPE_ROLLUP_READINESS_ARTIFACT_PATH, repoRoot)
  ?? resolve(repoRoot, 'tmp', 'session-recording', 'scope-rollup-readiness.json')

const READINESS_SCHEMA = 'SESSION_RECORDING_SCOPE_ROLLUP_READINESS_V1'

function sha256Hex(buffer) {
  return `sha256:${createHash('sha256').update(buffer).digest('hex')}`
}

function fail(message) {
  process.stderr.write(`bootstrap-source-bound-readiness: FAIL: ${message}\n`)
  process.exit(1)
}

function run(cmd, args) {
  // Always runs against scriptRepoRoot (uv.lock / pyproject.toml live
  // there); a --repo-root fixture generally has no uv project of its own.
  return execFileSync(cmd, args, { cwd: scriptRepoRoot, encoding: 'utf8', timeout: 60_000 })
}

function main() {
  // Step 1: locked sync of the default dependency group (pyyaml,
  // jsonschema) that the capture producer imports.
  try {
    run('uv', ['sync', '--locked'])
  } catch (err) {
    fail(`uv sync --locked failed: ${String(err?.message ?? err)}`)
  }

  // Step 2: resolve the fixed interpreter realpath/version uv would use for
  // a subsequent `uv run --no-sync`.
  let interpreterRealpath
  let interpreterVersion
  try {
    interpreterRealpath = run('uv', ['run', '--no-sync', 'python3', '-c', 'import sys; print(sys.executable)']).trim()
    interpreterVersion = run(interpreterRealpath, ['--version']).trim()
  } catch (err) {
    fail(`interpreter resolution failed: ${String(err?.message ?? err)}`)
    return
  }
  if (!interpreterRealpath || !existsSync(interpreterRealpath)) {
    fail('resolved interpreter path does not exist')
    return
  }

  // Step 3: import smoke — PyYAML (producer runtime dep) + py_compile of
  // the producer script itself (catches syntax errors before the hot path
  // ever spawns it).
  try {
    run(interpreterRealpath, ['-c', 'import yaml'])
  } catch (err) {
    fail(`PyYAML import smoke failed: ${String(err?.message ?? err)}`)
    return
  }
  try {
    run(interpreterRealpath, ['-m', 'py_compile', producerPath])
  } catch (err) {
    fail(`producer py_compile smoke failed: ${String(err?.message ?? err)}`)
    return
  }

  // Step 4: digests for binding (always read from scriptRepoRoot; see run()).
  let uvLockDigest = null
  const uvLockPath = resolve(scriptRepoRoot, 'uv.lock')
  if (existsSync(uvLockPath)) {
    uvLockDigest = sha256Hex(readFileSync(uvLockPath))
  }
  let pythonVersionDigest = null
  const pythonVersionPath = resolve(scriptRepoRoot, '.python-version')
  if (existsSync(pythonVersionPath)) {
    pythonVersionDigest = sha256Hex(readFileSync(pythonVersionPath))
  }
  let producerDigest
  try {
    producerDigest = sha256Hex(readFileSync(producerPath))
  } catch (err) {
    fail(`unable to digest producer script: ${String(err?.message ?? err)}`)
    return
  }

  const readiness = {
    schema: READINESS_SCHEMA,
    artifact_version: 1,
    repo_root_realpath: repoRoot,
    uv_lock_digest: uvLockDigest,
    python_version_digest: pythonVersionDigest,
    interpreter_realpath: interpreterRealpath,
    interpreter_version: interpreterVersion,
    producer_digest: producerDigest,
    prepared: true,
    generated_at: new Date().toISOString(),
  }

  try {
    writeReadinessAtomic(readinessPath, readiness)
  } catch (err) {
    fail(`readiness parent directory preparation failed: ${String(err?.message ?? err)}`)
    return
  }
  process.stdout.write(`bootstrap-source-bound-readiness: wrote ${readinessPath}\n`)
}

// Issue #2004 P1-1 / Issue #2029: validate and (only if newly created) chmod
// the parent directory of the fixed private readiness artifact, mirroring
// the Python eligibility producer's prepare_private_parent_dir() (see
// .claude/scripts/check_session_recording_runtime_safety.py). The previous
// implementation did an unconditional mkdirSync(..., {recursive: true}) +
// chmodSync(dir, 0o700) with no verification at all, so a pre-existing
// parent that is a symlink (or owned by someone else) would be silently
// forced to mode 0700. This instead:
//   - creates the parent (and any missing ancestors) with mode 0700 ONLY
//     when it does not already exist; the resulting path is then opened
//     and validated through the fd before fchmodSync(), the same as the
//     pre-existing-parent case below;
//   - for a pre-existing parent, opens it with O_RDONLY | O_DIRECTORY
//     (adding O_NOFOLLOW when the runtime exposes those constants) and
//     verifies -- via the open file descriptor, never the pathname
//     again, using fstatSync() -- that it is a real directory and, when
//     process.getuid() is available, that its uid matches the current
//     uid, before repairing its mode to exactly 0700 (a
//     looser mode left by an older version of this script is explicitly
//     repaired by policy, never silently trusted as-is).
//
// Issue #2029: O_DIRECTORY is now included whenever the runtime exposes
// fs.constants.O_DIRECTORY (mirroring the existing O_NOFOLLOW availability
// guard). Its purpose here is NOT reason classification -- it is what makes
// this open call itself non-blocking. Without O_DIRECTORY, opening a path
// whose final component is a FIFO (or any other special file that a bare
// open() would otherwise wait on, e.g. a device) can block INSIDE the
// open()/openSync() call indefinitely if there is no writer on the other
// end -- long before fstatSync().isDirectory() ever gets a chance to reject
// it. O_DIRECTORY makes the kernel fail the open itself the moment it
// resolves a non-directory final path component, before any FIFO/device-
// specific open() semantics run.
//
// Empirically (verified against the real syscall), combining O_DIRECTORY
// with O_NOFOLLOW means a trailing symlink -- even one pointing at a real,
// valid directory -- is ALSO reported as ENOTDIR, not ELOOP: the one flag
// needed to avoid blocking on a FIFO also makes the errno/code alone
// insufficient to tell a symlink apart from a genuine non-directory. Per
// Issue #2029 guidance, the specific error code is NOT a portable
// reject-vs-accept contract either way (both cases reject just the same);
// ELOOP is still checked first for runtimes/kernels that DO report it, and
// ENOTDIR falls back to a non-blocking, non-authoritative `lstatSync()`
// (see `diagnoseParentIsSymlink()`) purely to choose a more specific
// diagnostic reason for an ALREADY-rejected open -- never to reopen with
// weaker flags or retry after following the symlink.
//
// Issue #2028: ELOOP is NOT exclusively caused by a trailing symlink -- a
// circular (or merely very long, non-circular) symlink chain earlier in the
// path PREFIX also fails with ELOOP, and on such a path `lstatSync(dir)`
// itself fails to resolve (it must walk the very same broken prefix) rather
// than confirming a trailing symlink. `diagnoseParentIsSymlink()` is used
// for ELOOP the same way it already was for ENOTDIR: purely to decide
// whether THIS specific rejection may be reported as the more specific
// `parent_is_symlink` reason. When it cannot confirm a trailing symlink
// (either because `dir` genuinely is not one, or because the auxiliary
// `lstatSync()` call itself failed and could not tell), the ELOOP is
// reported as `parent_unavailable` instead -- never asserted as a symlink
// loop that was never actually confirmed.
function diagnoseParentIsSymlink(dir) {
  try {
    return lstatSync(dir).isSymbolicLink()
  } catch {
    return false
  }
}

// Issue #2028: both `mkdirSync()` (only reached when `dir` does not already
// exist) and `openSync()` (always reached afterward) can fail with the same
// family of errnos coming from the same underlying path-resolution
// machinery, so both funnel through this single classifier -- never two
// independently-drifting copies of the same ELOOP/ENOTDIR reasoning. `op`
// (`'mkdirSync'` or `'openSync'`) and `err.code` are always attached to the
// thrown `PrivateParentDirError` (never swallowed) so callers/CLI output can
// report exactly which syscall failed with which errno, in addition to the
// (possibly-unconfirmed) diagnostic reason. This always throws.
function classifyAndThrowParentDirFailure(dir, err, op) {
  const code = (err && err.code) || null
  if (code === 'ELOOP') {
    if (diagnoseParentIsSymlink(dir)) {
      throw new PrivateParentDirError('parent_is_symlink', { errno: code, op, cause: err, path: dir })
    }
    // Confirmed NOT a trailing symlink, or the auxiliary lstatSync() itself
    // could not tell (e.g. it failed trying to resolve the very same broken
    // path prefix) -- either way, an unconfirmed ELOOP must never be
    // reported as a symlink loop. The original errno/op are still attached
    // above; only the diagnostic reason is downgraded to the generic,
    // already-existing `parent_unavailable`.
    throw new PrivateParentDirError('parent_unavailable', { errno: code, op, cause: err, path: dir })
  }
  if (code === 'ENOTDIR') {
    if (diagnoseParentIsSymlink(dir)) {
      throw new PrivateParentDirError('parent_is_symlink', { errno: code, op, cause: err, path: dir })
    }
    throw new PrivateParentDirError('parent_not_a_directory', { errno: code, op, cause: err, path: dir })
  }
  throw new PrivateParentDirError('parent_unavailable', { errno: code, op, cause: err, path: dir })
}

function preparePrivateParentDir(dir) {
  if (!existsSync(dir)) {
    // Issue #2028: previously uncaught -- any mkdirSync() failure (e.g. a
    // circular or long non-circular symlink chain in the path PREFIX
    // failing with ELOOP) propagated as a raw, undiagnosed exception. This
    // now runs the SAME classification openSync() failures already used
    // below, so a path-prefix ELOOP is diagnosed (and, when unconfirmed as
    // a trailing symlink, NOT asserted as one) at the point it actually
    // occurs, rather than only being caught later at openSync() (which,
    // for a mkdirSync failure, is never even reached).
    try {
      mkdirSync(dir, { recursive: true, mode: 0o700 })
    } catch (err) {
      classifyAndThrowParentDirFailure(dir, err, 'mkdirSync')
    }
  }

  let flags = fsConstants.O_RDONLY
  if (typeof fsConstants.O_DIRECTORY === 'number') flags |= fsConstants.O_DIRECTORY
  if (typeof fsConstants.O_NOFOLLOW === 'number') flags |= fsConstants.O_NOFOLLOW

  let fd
  try {
    fd = openSync(dir, flags)
  } catch (err) {
    classifyAndThrowParentDirFailure(dir, err, 'openSync')
  }
  try {
    const st = fstatSync(fd)
    if (!st.isDirectory()) {
      // Defense in depth for a runtime lacking fs.constants.O_DIRECTORY:
      // the open() above could not fail fast on a non-directory, so this
      // fstatSync()-based check is the only thing rejecting it. Not an
      // errno-bearing failure (open() itself succeeded), so no errno/op is
      // attached.
      throw new PrivateParentDirError('parent_not_a_directory', { op: 'fstatSync' })
    }
    if (typeof process.getuid === 'function' && st.uid !== process.getuid()) {
      throw new PrivateParentDirError('parent_owner_mismatch', { op: 'fstatSync' })
    }
    fchmodSync(fd, 0o700)
  } finally {
    closeSync(fd)
  }
}

function writeReadinessAtomic(targetPath, payload) {
  const dir = dirname(targetPath)
  preparePrivateParentDir(dir)
  const tmpPath = `${targetPath}.tmp.${process.pid}`
  const rendered = `${JSON.stringify(payload, null, 2)}\n`
  const fd = openSync(tmpPath, 'wx', 0o600)
  try {
    writeFileSync(fd, rendered, { encoding: 'utf8' })
    fchmodSync(fd, 0o600)
  } finally {
    closeSync(fd)
  }
  renameSync(tmpPath, targetPath)
}

main()
