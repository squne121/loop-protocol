#!/usr/bin/env node
/**
 * collect-playtest-env.mjs
 *
 * ローカル CLI 用のプレイテスト環境メタデータ採取スクリプト (AC1)
 *
 * Usage:
 *   node scripts/collect-playtest-env.mjs          # YAML 出力 (デフォルト)
 *   node scripts/collect-playtest-env.mjs --json   # JSON 出力
 *
 * 出力項目:
 *   - executed_at  (ISO 8601)
 *   - tested_commit (git rev-parse HEAD)
 *   - node_version
 *   - pnpm_version
 *   - platform / os
 */

import { execSync } from 'node:child_process'
import os from 'node:os'
import process from 'node:process'

// ---------------------------------------------------------------------------
// Collect
// ---------------------------------------------------------------------------

function tryExec(cmd) {
  try {
    return execSync(cmd, { encoding: 'utf8', stdio: ['pipe', 'pipe', 'pipe'] }).trim()
  } catch {
    return null
  }
}

function collectEnv() {
  const executed_at = new Date().toISOString()

  const tested_commit =
    tryExec('git rev-parse HEAD') ??
    tryExec('git rev-parse --short HEAD') ??
    'unknown'

  const commit_unknown_reason =
    tested_commit === 'unknown' ? 'git rev-parse HEAD が失敗しました（git リポジトリ外または git 未インストール）' : undefined

  const node_version = process.version

  const pnpm_version = tryExec('pnpm --version') ?? 'unknown'

  const platform = os.platform()
  const os_release = os.release()
  const os_type = os.type()
  const arch = os.arch()

  return {
    executed_at,
    tested_commit,
    ...(commit_unknown_reason ? { commit_unknown_reason } : {}),
    node_version,
    pnpm_version,
    platform,
    os_type,
    os_release,
    arch,
  }
}

// ---------------------------------------------------------------------------
// Output
// ---------------------------------------------------------------------------

function toYamlLine(key, val, indent = 0) {
  const pad = '  '.repeat(indent)
  if (val === null || val === undefined) return `${pad}${key}: null`
  if (typeof val === 'string') {
    if (val.includes(':') || val.includes('#') || val.includes('\n')) {
      return `${pad}${key}: "${val.replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/\n/g, '\\n')}"`
    }
    return `${pad}${key}: ${val}`
  }
  return `${pad}${key}: ${val}`
}

function toYaml(data) {
  const lines = ['# Loop Protocol — Playtest Environment Metadata (AC1)', '']
  for (const [k, v] of Object.entries(data)) {
    lines.push(toYamlLine(k, v))
  }
  return lines.join('\n') + '\n'
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

const args = process.argv.slice(2)
const useJson = args.includes('--json')

const data = collectEnv()

if (useJson) {
  process.stdout.write(JSON.stringify(data, null, 2) + '\n')
} else {
  process.stdout.write(toYaml(data))
}
