import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { spawnSync } from 'node:child_process'

const root = resolve(import.meta.dirname, '..', '..')
const hooks = JSON.parse(readFileSync(resolve(root, '.codex/hooks.json'), 'utf8')).hooks

function invoke(event: 'SessionEnd' | 'SubagentStop') {
  const command = hooks[event][0].hooks[0].command.split(' ')
  return spawnSync(command[0], command.slice(1), {
    cwd: root,
    input: '{}',
    encoding: 'utf8',
    env: { ...process.env, CODEX_PASSIVE_RECORDING_DIR: '/dev/null' },
  })
}

describe('quarantine 後の Codex hook stdout 契約', () => {
  it('active hook は SessionEnd と SubagentStop のみ', () => {
    expect(Object.keys(hooks).sort()).toEqual(['SessionEnd', 'SubagentStop'])
  })

  it('SessionEnd は stdout を出力しない', () => {
    const result = invoke('SessionEnd')
    expect(result.status).toBe(0)
    expect(result.stdout).toBe('')
  })

  it('SubagentStop は closed continuation JSON だけを返す', () => {
    const result = invoke('SubagentStop')
    expect(result.status).toBe(0)
    expect(JSON.parse(result.stdout)).toEqual({ continue: true })
    expect(result.stdout).toBe('{"continue":true}')
  })
})
