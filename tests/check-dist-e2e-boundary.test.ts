import { execFileSync, spawnSync } from 'node:child_process'
import { mkdtempSync, mkdirSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'

import { afterEach, describe, expect, it } from 'vitest'

import {
  parseArguments,
  scanArtifactTree,
  verifyDistE2EBoundary,
} from '../scripts/check-dist-e2e-boundary.mjs'
import {
  e2eControlMarkers,
  validateE2EControlMarkerManifest,
} from '../scripts/e2e-control-marker-manifest.mjs'

const checkerPath = resolve('scripts/check-dist-e2e-boundary.mjs')
const temporaryRoots: string[] = []

function makeDist() {
  const root = mkdtempSync(join(tmpdir(), 'issue-1425-dist-'))
  temporaryRoots.push(root)
  return root
}

function runChecker(args: string[]) {
  return spawnSync(process.execPath, [checkerPath, ...args], { encoding: 'utf8' })
}

function fakeStat(kind: 'directory' | 'file' | 'socket') {
  return {
    isDirectory: () => kind === 'directory',
    isFile: () => kind === 'file',
    isSymbolicLink: () => false,
  }
}

afterEach(() => {
  for (const root of temporaryRoots.splice(0)) {
    rmSync(root, { force: true, recursive: true })
  }
})

describe('E2E control marker manifest', () => {
  it('GIVEN the manifest WHEN inspected THEN it is the required independent exact set and classifications', () => {
    expect(e2eControlMarkers).toHaveLength(6)
    expect(e2eControlMarkers.map(({ name }) => name)).toEqual([
      '__LOOP_E2E__',
      '__LOOP_E2E_BOOTSTRAP__',
      '__LOOP_VISUAL_SCENARIO__',
      '__LOOP_STORAGE_KEY__',
      '__E2E_SHORT_SORTIE__',
      '__E2E_PLAYER_HP_OVERRIDE__',
    ])
    expect(e2eControlMarkers.every(({ productionForbidden, requiredInE2E }) => (
      productionForbidden && requiredInE2E
    ))).toBe(true)
  })

  it('GIVEN an empty or duplicate manifest WHEN validated THEN it fails closed', () => {
    expect(() => validateE2EControlMarkerManifest([])).toThrow('non-empty array')
    expect(() => validateE2EControlMarkerManifest([
      { name: '__LOOP_E2E__', productionForbidden: true, requiredInE2E: true },
      { name: '__LOOP_E2E__', productionForbidden: true, requiredInE2E: true },
    ])).toThrow('duplicate marker')
    expect(() => validateE2EControlMarkerManifest([
      { name: '', productionForbidden: true, requiredInE2E: true },
    ])).toThrow('non-empty name')
  })
})

describe('check-dist-e2e-boundary', () => {
  it('GIVEN production artifacts contain multiple control markers WHEN checked THEN it fails with stable relative diagnostics', () => {
    const root = makeDist()
    mkdirSync(join(root, 'nested'))
    writeFileSync(join(root, 'z.js'), '__LOOP_E2E__')
    writeFileSync(join(root, 'nested', 'a.map'), '__LOOP_VISUAL_SCENARIO__')

    const result = runChecker(['--mode', 'production', '--dist', root])

    expect(result.status).toBe(1)
    expect(result.stderr).toContain('__LOOP_E2E__: test-root/z.js')
    expect(result.stderr).toContain('__LOOP_VISUAL_SCENARIO__: test-root/nested/a.map')
    expect(result.stderr).not.toContain(root)
  })

  it('GIVEN an E2E artifact misses one required marker WHEN checked THEN it fails', () => {
    const root = makeDist()
    writeFileSync(join(root, 'bundle.js'), e2eControlMarkers.slice(0, -1).map(({ name }) => name).join('\n'))

    const result = runChecker(['--mode', 'e2e', '--dist', root])

    expect(result.status).toBe(1)
    expect(result.stderr).toContain('missing marker: __E2E_PLAYER_HP_OVERRIDE__')
  })

  it('GIVEN clean production and complete nested E2E artifacts WHEN checked THEN both pass', async () => {
    const production = makeDist()
    writeFileSync(join(production, 'bundle.js'), 'production artifact')
    await expect(verifyDistE2EBoundary({ mode: 'production', distPath: production })).resolves.toBeUndefined()

    const e2e = makeDist()
    mkdirSync(join(e2e, 'nested'))
    writeFileSync(join(e2e, 'nested', 'bundle.js'), e2eControlMarkers.map(({ name }) => name).join('\n'))
    await expect(verifyDistE2EBoundary({ mode: 'e2e', distPath: e2e })).resolves.toBeUndefined()
  })

  it('GIVEN missing, empty, regular-file, symlink, or FIFO roots WHEN checked THEN each fails closed', () => {
    const missing = join(tmpdir(), `issue-1425-missing-${Date.now()}`)
    expect(runChecker(['--mode', 'production', '--dist', missing]).status).toBe(1)

    const empty = makeDist()
    expect(runChecker(['--mode', 'production', '--dist', empty]).status).toBe(1)

    const regular = makeDist()
    const regularFile = join(regular, 'dist-file')
    writeFileSync(regularFile, 'not a directory')
    expect(runChecker(['--mode', 'production', '--dist', regularFile]).status).toBe(1)

    const symlinkRoot = makeDist()
    const target = makeDist()
    writeFileSync(join(target, 'bundle.js'), 'clean')
    symlinkSync(target, join(symlinkRoot, 'linked-dist'))
    expect(runChecker(['--mode', 'production', '--dist', join(symlinkRoot, 'linked-dist')]).status).toBe(1)

    if (process.platform !== 'win32') {
      const fifo = join(symlinkRoot, 'control.fifo')
      execFileSync('mkfifo', [fifo])
      expect(runChecker(['--mode', 'production', '--dist', symlinkRoot]).status).toBe(1)
    }
  })

  it('GIVEN a list, stat, read, or special-node error WHEN scanning THEN it fails closed', async () => {
    const virtualRoot = '/virtual-dist'
    await expect(scanArtifactTree(virtualRoot, {
      lstat: async (path: string) => path === virtualRoot ? fakeStat('directory') : fakeStat('socket'),
      readdir: async () => ['socket'],
      readFile: async () => Buffer.from(''),
    })).rejects.toThrow('unsupported filesystem node')
    await expect(scanArtifactTree(virtualRoot, {
      lstat: async () => fakeStat('directory'),
      readdir: async () => { throw new Error('denied') },
      readFile: async () => Buffer.from(''),
    })).rejects.toThrow('unreadable directory')
    await expect(scanArtifactTree(virtualRoot, {
      lstat: async (path: string) => path === virtualRoot ? fakeStat('directory') : fakeStat('file'),
      readdir: async () => ['bundle.js'],
      readFile: async () => { throw new Error('denied') },
    })).rejects.toThrow('unreadable regular file')
  })

  it('GIVEN unknown or incomplete CLI arguments WHEN parsed THEN it reports a usage error', () => {
    expect(() => parseArguments([])).toThrow('usage:')
    expect(() => parseArguments(['--mode', 'unknown', '--dist', 'dist'])).toThrow('usage:')
    expect(() => parseArguments(['--dist', 'dist', '--mode', 'production'])).toThrow('usage:')
  })

  it('GIVEN real production, E2E, and preview-namespace builds WHEN checked THEN their boundaries are enforced', async () => {
    const baseEnv = { ...process.env }
    execFileSync('pnpm', ['build'], { cwd: resolve('.'), env: { ...baseEnv, VITE_E2E_MODE: 'false' } })
    await expect(verifyDistE2EBoundary({ mode: 'production', distPath: resolve('dist') })).resolves.toBeUndefined()

    execFileSync('pnpm', ['build'], { cwd: resolve('.'), env: { ...baseEnv, VITE_E2E_MODE: 'true' } })
    await expect(verifyDistE2EBoundary({ mode: 'e2e', distPath: resolve('dist') })).resolves.toBeUndefined()

    execFileSync('pnpm', ['build'], {
      cwd: resolve('.'),
      env: { ...baseEnv, VITE_E2E_MODE: 'false', VITE_LOOP_STORAGE_NAMESPACE: 'pr-1425' },
    })
    await expect(verifyDistE2EBoundary({ mode: 'production', distPath: resolve('dist') })).resolves.toBeUndefined()
  }, 60_000)
})
