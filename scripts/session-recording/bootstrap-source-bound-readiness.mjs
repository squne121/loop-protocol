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

// Issue #2004 P1-1: validate and (only if newly created) chmod the parent
// directory of the fixed private readiness artifact, mirroring the Python
// eligibility producer's prepare_private_parent_dir() (see
// .claude/scripts/check_session_recording_runtime_safety.py). The previous
// implementation did an unconditional mkdirSync(..., {recursive: true}) +
// chmodSync(dir, 0o700) with no verification at all, so a pre-existing
// parent that is a symlink (or owned by someone else) would be silently
// forced to mode 0700. This instead:
//   - creates the parent (and any missing ancestors) with mode 0700 ONLY
//     when it does not already exist; the resulting path is then opened
//     and validated through the fd before fchmodSync(), the same as the
//     pre-existing-parent case below;
//   - for a pre-existing parent, opens it with O_RDONLY (adding
//     O_NOFOLLOW when the runtime exposes fs.constants.O_NOFOLLOW) and
//     verifies -- via the open file descriptor, never the pathname
//     again, using fstatSync() -- that it is a real directory and, when
//     process.getuid() is available, that its uid matches the current
//     uid, before repairing its mode to exactly 0700 (a
//     looser mode left by an older version of this script is explicitly
//     repaired by policy, never silently trusted as-is).
function preparePrivateParentDir(dir) {
  if (!existsSync(dir)) {
    mkdirSync(dir, { recursive: true, mode: 0o700 })
  }

  // Deliberately NOT combining O_DIRECTORY with O_NOFOLLOW here; this
  // function does not rely on any specific errno precedence between them.
  // O_NOFOLLOW is added to flags only when the runtime exposes
  // fs.constants.O_NOFOLLOW, and only rejects a symlink at the trailing
  // (basename) path component -- it does not affect earlier path
  // components. Whether the opened path is actually a directory is
  // verified separately below via fstatSync().isDirectory() against the
  // fd of whatever was actually opened, not via the open() flags alone.
  let flags = fsConstants.O_RDONLY
  if (typeof fsConstants.O_NOFOLLOW === 'number') flags |= fsConstants.O_NOFOLLOW

  let fd
  try {
    fd = openSync(dir, flags)
  } catch (err) {
    if (err && err.code === 'ELOOP') {
      throw new Error('parent_is_symlink', { cause: err })
    }
    throw new Error('parent_unavailable', { cause: err })
  }
  try {
    const st = fstatSync(fd)
    if (!st.isDirectory()) {
      throw new Error('parent_not_a_directory')
    }
    if (typeof process.getuid === 'function' && st.uid !== process.getuid()) {
      throw new Error('parent_owner_mismatch')
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
