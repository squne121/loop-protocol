#!/usr/bin/env node

import { lstat, readFile, readdir } from 'node:fs/promises'
import { relative, resolve, sep } from 'node:path'

import { e2eControlMarkers, validateE2EControlMarkerManifest } from './e2e-control-marker-manifest.mjs'

const MARKERS = validateE2EControlMarkerManifest(e2eControlMarkers)

class BoundaryError extends Error {}

function posixPath(value) {
  return value.split(sep).join('/')
}

function displayRoot(root) {
  const repoRelative = relative(process.cwd(), root)
  if (repoRelative && !repoRelative.startsWith(`..${sep}`) && repoRelative !== '..') {
    return posixPath(repoRelative)
  }
  return 'test-root'
}

function displayPath(root, candidate) {
  const rootLabel = displayRoot(root)
  const nested = relative(root, candidate)
  return nested ? `${rootLabel}/${posixPath(nested)}` : rootLabel
}

function unsupportedPath(display) {
  return new BoundaryError(`unsupported filesystem node: ${display}`)
}

async function inspectPath(root, candidate, fsOps, files) {
  const display = displayPath(root, candidate)
  let stat
  try {
    stat = await fsOps.lstat(candidate)
  } catch {
    throw new BoundaryError(`unreadable filesystem path: ${display}`)
  }

  if (stat.isSymbolicLink()) {
    throw unsupportedPath(display)
  }
  if (stat.isFile()) {
    files.push({ absolutePath: candidate, displayPath: display })
    return
  }
  if (!stat.isDirectory()) {
    throw unsupportedPath(display)
  }

  let entries
  try {
    entries = await fsOps.readdir(candidate)
  } catch {
    throw new BoundaryError(`unreadable directory: ${display}`)
  }
  entries.sort((left, right) => (left < right ? -1 : left > right ? 1 : 0))
  for (const entry of entries) {
    await inspectPath(root, `${candidate}${sep}${entry}`, fsOps, files)
  }
}

export async function scanArtifactTree(distPath, fsOps = { lstat, readFile, readdir }) {
  const root = resolve(distPath)
  let rootStat
  try {
    rootStat = await fsOps.lstat(root)
  } catch {
    throw new BoundaryError(`missing or unreadable dist root: ${displayPath(root, root)}`)
  }
  if (rootStat.isSymbolicLink() || !rootStat.isDirectory()) {
    throw new BoundaryError(`dist root must be a real directory: ${displayPath(root, root)}`)
  }

  const files = []
  await inspectPath(root, root, fsOps, files)
  if (files.length === 0) {
    throw new BoundaryError(`empty dist tree: ${displayPath(root, root)}`)
  }

  const matches = []
  for (const file of files) {
    let content
    try {
      content = await fsOps.readFile(file.absolutePath)
    } catch {
      throw new BoundaryError(`unreadable regular file: ${file.displayPath}`)
    }
    for (const marker of MARKERS) {
      if (content.includes(marker.name)) {
        matches.push({ marker: marker.name, path: file.displayPath })
      }
    }
  }
  matches.sort((left, right) => {
    const leftKey = `${left.marker}\u0000${left.path}`
    const rightKey = `${right.marker}\u0000${right.path}`
    return leftKey < rightKey ? -1 : leftKey > rightKey ? 1 : 0
  })
  return matches
}

export async function verifyDistE2EBoundary({ mode, distPath }) {
  const matches = await scanArtifactTree(distPath)
  if (mode === 'production') {
    if (matches.length > 0) {
      throw new BoundaryError(matches.map(({ marker, path }) => `${marker}: ${path}`).join('\n'))
    }
    return
  }
  if (mode === 'e2e') {
    const found = new Set(matches.map(({ marker }) => marker))
    const missing = MARKERS.filter(({ name, requiredInE2E }) => requiredInE2E && !found.has(name))
    if (missing.length > 0) {
      throw new BoundaryError(missing.map(({ name }) => `missing marker: ${name}`).join('\n'))
    }
    return
  }
  throw new BoundaryError('usage: check-dist-e2e-boundary --mode production|e2e --dist <path>')
}

export function parseArguments(args) {
  if (args.length !== 4 || args[0] !== '--mode' || args[2] !== '--dist') {
    throw new BoundaryError('usage: check-dist-e2e-boundary --mode production|e2e --dist <path>')
  }
  if (args[1] !== 'production' && args[1] !== 'e2e') {
    throw new BoundaryError('usage: check-dist-e2e-boundary --mode production|e2e --dist <path>')
  }
  if (!args[3]) {
    throw new BoundaryError('usage: check-dist-e2e-boundary --mode production|e2e --dist <path>')
  }
  return { mode: args[1], distPath: args[3] }
}

async function main() {
  try {
    await verifyDistE2EBoundary(parseArguments(process.argv.slice(2)))
  } catch (error) {
    process.stderr.write(`ERROR: ${error instanceof Error ? error.message : 'boundary check failed'}\n`)
    process.exitCode = 1
  }
}

if (import.meta.url === `file://${process.argv[1]}`) {
  await main()
}
