import { afterEach, describe, expect, it } from 'vitest'
import { readFileSync, existsSync } from 'fs'
import { resolve } from 'path'

import { writeJsonAtomic } from '../../scripts/agent-logs/lib/atomic-json.mjs'
import { cleanupTempDir, createTempDir } from './helpers'

const tempDirs: string[] = []

afterEach(() => {
  while (tempDirs.length > 0) {
    cleanupTempDir(tempDirs.pop() as string)
  }
})

describe('writeJsonAtomic', () => {
  it('GIVEN concurrent writers WHEN publishing the same path THEN exactly one succeeds and the other fails closed', async () => {
    const tempDir = createTempDir()
    tempDirs.push(tempDir)
    const outputPath = resolve(tempDir, 'shared.json')

    const results = await Promise.allSettled([
      writeJsonAtomic(outputPath, { writer: 'a' }),
      writeJsonAtomic(outputPath, { writer: 'b' }),
    ])

    const fulfilled = results.filter((result) => result.status === 'fulfilled')
    const rejected = results.filter((result) => result.status === 'rejected')
    expect(fulfilled).toHaveLength(1)
    expect(rejected).toHaveLength(1)
    expect(String(rejected[0].reason?.code ?? rejected[0].reason?.message)).toContain('output.exists')

    const file = JSON.parse(readFileSync(outputPath, 'utf-8'))
    expect(['a', 'b']).toContain(file.writer)
  })

  it('GIVEN a failure before publish WHEN writeJsonAtomic aborts THEN no zero-byte final file is left behind', async () => {
    const tempDir = createTempDir()
    tempDirs.push(tempDir)
    const outputPath = resolve(tempDir, 'crash.json')

    await expect(writeJsonAtomic(outputPath, { writer: 'a' }, {
      afterTempSync: async () => {
        throw new Error('forced_publish_failure')
      },
    })).rejects.toThrow('forced_publish_failure')

    expect(existsSync(outputPath)).toBe(false)
  })
})
