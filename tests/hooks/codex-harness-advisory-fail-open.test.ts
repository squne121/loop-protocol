import { describe, expect, it } from 'vitest'
import { invoke } from './codex-harness-advisory-recorder.test'

describe('Codex passive recorder fail-open behavior', () => {
  it.each(['{malformed', JSON.stringify({ transcript: 'ignored' })])(
    'SubagentStop returns the exact closed continuation JSON',
    (payload) => {
      const result = invoke('SubagentStop', payload)
      expect(result.status).toBe(0)
      expect(result.stdout).toBe('{"continue":true}')
      expect(result.stderr).toBe('')
    },
  )

  it('fails open when its recording destination is unwritable', () => {
    const result = invoke('SubagentStop', '{}', '/dev/null/not-a-directory')
    expect(result.status).toBe(0)
    expect(result.stdout).toBe('{"continue":true}')
  })
})
