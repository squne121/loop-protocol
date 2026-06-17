import { execFileSync } from 'child_process'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'fs'
import { resolve } from 'path'
import { tmpdir } from 'os'

export const REPO_ROOT = resolve(__dirname, '..', '..')
export const START_SCRIPT = resolve(REPO_ROOT, 'scripts', 'agent-logs', 'start-agent-run.mjs')
export const FINALIZE_SCRIPT = resolve(REPO_ROOT, 'scripts', 'agent-logs', 'finalize-agent-run.mjs')
export const PACKAGE_JSON = resolve(REPO_ROOT, 'package.json')

export function createTempDir() {
  return mkdtempSync(resolve(tmpdir(), 'agent-logs-'))
}

export function cleanupTempDir(tempDir: string) {
  rmSync(tempDir, { recursive: true, force: true })
}

export function readJson(path: string) {
  return JSON.parse(readFileSync(path, 'utf-8'))
}

export function writeJson(path: string, value: unknown) {
  writeFileSync(path, JSON.stringify(value, null, 2))
}

export function runNodeScript(scriptPath: string, args: string[]) {
  try {
    const stdout = execFileSync(process.execPath, [scriptPath, ...args], {
      cwd: REPO_ROOT,
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
    })
    return { exitCode: 0, stdout, stderr: '' }
  } catch (error) {
    const err = error as { status?: number; stdout?: string; stderr?: string }
    return {
      exitCode: err.status ?? 1,
      stdout: err.stdout || '',
      stderr: err.stderr || '',
    }
  }
}

export function createDraftArgs(outputPath: string) {
  return [
    '--output', outputPath,
    '--run-id', 'run-936-001',
    '--target-kind', 'issue',
    '--target-id', '936',
    '--phase', 'implementation',
    '--actor-type', 'ai_agent',
    '--actor-name', 'Codex worker',
    '--started-at', '2026-06-17T12:00:00.000Z',
  ]
}

export function createCommandSummaryJson(overrides: Record<string, unknown> = {}) {
  return JSON.stringify({
    command_label: 'pnpm test -- tests/agent-logs',
    exit_code: 0,
    verdict: 'pass',
    summary: 'focused agent-logs tests passed',
    artifact_ref: 'artifact:agent-logs-tests',
    ...overrides,
  })
}

export function createManifestRefJson() {
  return JSON.stringify({
    kind: 'manifest_digest',
    artifact_id: null,
    artifact_digest: null,
    workflow_run_url: null,
    schema_ref: null,
    ref: null,
    digest: 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    validation_verdict: 'unknown',
  })
}

export function createEvidenceRefJson() {
  return JSON.stringify({
    kind: 'workflow_run',
    artifact_id: null,
    artifact_digest: null,
    workflow_run_url: null,
    schema_ref: null,
    ref: 'https://github.com/squne121/loop-protocol/actions/runs/123456',
    digest: 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
    validation_verdict: 'unknown',
  })
}

export function createDocReadRefJson() {
  return JSON.stringify({
    ref_kind: 'issue',
    ref: 'https://github.com/squne121/loop-protocol/issues/936',
    summary: 'implementation contract reviewed',
  })
}

export function createFinalizeArgs(draftPath: string, outputPath: string, extras: string[] = []) {
  return [
    '--draft', draftPath,
    '--output', outputPath,
    '--checked-at', '2026-06-17T12:30:00.000Z',
    '--command-summary-json', createCommandSummaryJson(),
    '--manifest-ref-json', createManifestRefJson(),
    '--evidence-ref-json', createEvidenceRefJson(),
    '--doc-read-ref-json', createDocReadRefJson(),
    ...extras,
  ]
}
