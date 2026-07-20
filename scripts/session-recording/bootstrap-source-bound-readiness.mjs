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
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync, openSync, closeSync, fchmodSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const repoRoot = resolve(__dirname, '..', '..')
const producerPath = resolve(repoRoot, '.claude', 'hooks', 'capture_scope_rollup_final_response.py')
const readinessPath = process.env.SCOPE_ROLLUP_READINESS_ARTIFACT_PATH
  ? resolve(process.env.SCOPE_ROLLUP_READINESS_ARTIFACT_PATH)
  : resolve(repoRoot, '.claude', 'tmp', 'session-recording', 'scope-rollup-readiness.json')

const READINESS_SCHEMA = 'SESSION_RECORDING_SCOPE_ROLLUP_READINESS_V1'

function sha256Hex(buffer) {
  return `sha256:${createHash('sha256').update(buffer).digest('hex')}`
}

function fail(message) {
  process.stderr.write(`bootstrap-source-bound-readiness: FAIL: ${message}\n`)
  process.exit(1)
}

function run(cmd, args) {
  return execFileSync(cmd, args, { cwd: repoRoot, encoding: 'utf8', timeout: 60_000 })
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

  // Step 4: digests for binding.
  let uvLockDigest = null
  const uvLockPath = resolve(repoRoot, 'uv.lock')
  if (existsSync(uvLockPath)) {
    uvLockDigest = sha256Hex(readFileSync(uvLockPath))
  }
  let pythonVersionDigest = null
  const pythonVersionPath = resolve(repoRoot, '.python-version')
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

  writeReadinessAtomic(readinessPath, readiness)
  process.stdout.write(`bootstrap-source-bound-readiness: wrote ${readinessPath}\n`)
}

function writeReadinessAtomic(targetPath, payload) {
  mkdirSync(dirname(targetPath), { recursive: true })
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
