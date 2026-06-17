import { link, mkdir, open, rm } from 'fs/promises'
import { basename, dirname, resolve } from 'path'
import { randomUUID } from 'crypto'

import { runtimeError } from './args.mjs'

function stableStringify(value) {
  return `${JSON.stringify(value, null, 2)}\n`
}

async function fsyncDirectory(directoryPath) {
  let directoryHandle
  try {
    directoryHandle = await open(directoryPath, 'r')
    await directoryHandle.sync()
  } catch {
    // best effort only
  } finally {
    await directoryHandle?.close()
  }
}

export async function writeJsonAtomic(outputPath, value, hooks = {}) {
  const resolvedOutputPath = resolve(outputPath)
  const outputDirectory = dirname(resolvedOutputPath)
  const temporaryPath = resolve(
    outputDirectory,
    `.${basename(resolvedOutputPath)}.${process.pid}.${randomUUID()}.tmp`
  )

  await mkdir(outputDirectory, { recursive: true })

  let temporaryHandle
  try {
    temporaryHandle = await open(temporaryPath, 'wx', 0o600)
    await temporaryHandle.writeFile(stableStringify(value), { encoding: 'utf-8' })
    await temporaryHandle.sync()
    await temporaryHandle.close()
    temporaryHandle = null
    await hooks.afterTempSync?.(temporaryPath, resolvedOutputPath)
    await link(temporaryPath, resolvedOutputPath)
    await hooks.afterPublishLink?.(temporaryPath, resolvedOutputPath)
    await fsyncDirectory(outputDirectory)
    await rm(temporaryPath, { force: true })
    await fsyncDirectory(outputDirectory)
  } catch (error) {
    await temporaryHandle?.close().catch(() => {})
    await rm(temporaryPath, { force: true }).catch(() => {})
    if (error && typeof error === 'object' && error.code === 'EEXIST') {
      throw runtimeError('output.exists', 'refusing to overwrite an existing output file')
    }
    throw error
  }
}
