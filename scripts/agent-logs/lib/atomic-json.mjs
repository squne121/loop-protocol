import { constants, open, rename, rm } from 'fs/promises'
import { dirname, join, basename } from 'path'
import { randomUUID } from 'crypto'

function stableStringify(value) {
  return `${JSON.stringify(value, null, 2)}\n`
}

async function fsyncDirectory(filePath) {
  const dirHandle = await open(dirname(filePath), constants.O_RDONLY)
  try {
    await dirHandle.sync()
  } finally {
    await dirHandle.close()
  }
}

export async function writeJsonAtomic(filePath, value) {
  const dir = dirname(filePath)
  const tempPath = join(
    dir,
    `.${basename(filePath)}.${process.pid}.${randomUUID()}.tmp`,
  )
  const payload = stableStringify(value)
  const handle = await open(tempPath, constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY, 0o600)

  try {
    await handle.writeFile(payload, { encoding: 'utf-8' })
    await handle.sync()
  } catch (error) {
    await handle.close().catch(() => {})
    await rm(tempPath, { force: true }).catch(() => {})
    throw error
  }

  await handle.close()

  try {
    const destination = await open(filePath, constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY, 0o600)
    await destination.close()
    await rm(filePath, { force: true })
  } catch (error) {
    await rm(tempPath, { force: true }).catch(() => {})
    if (error && typeof error === 'object' && 'code' in error && error.code === 'EEXIST') {
      const existsError = new Error('output_exists')
      existsError.code = 'OUTPUT_EXISTS'
      throw existsError
    }
    throw error
  }

  try {
    await rename(tempPath, filePath)
    await fsyncDirectory(filePath)
  } catch (error) {
    await rm(tempPath, { force: true }).catch(() => {})
    throw error
  }
}
