import { gameSnapshotSchemaVersion, type GameSnapshot } from '../state'

export const defaultSaveKey = 'loop-protocol.mvp.save'

const RESOURCE_CAP = 9_999_999
const DEFAULT_WEAPON_POWER = 1
const DEFAULT_PLAYER_MAX_HP = 8

type SnapshotRecord = Record<string, unknown>
type StorageAdapter = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>

export type LoadSuccessReason = 'loaded' | 'empty'
export type LoadFailureReason =
  | 'storage-unavailable'
  | 'read-error'
  | 'corrupt-json'
  | 'unsupported-schema'
  | 'invalid-schema'
export type SaveSuccessReason = 'saved'
export type SaveFailureReason = 'storage-unavailable' | 'write-error'

export type LoadResult =
  | {
      ok: true
      snapshot: GameSnapshot | null
      reason: LoadSuccessReason
    }
  | {
      ok: false
      snapshot: null
      reason: LoadFailureReason
      errorName?: string
    }

export type SaveResult =
  | {
      ok: true
      reason: SaveSuccessReason
    }
  | {
      ok: false
      reason: SaveFailureReason
      errorName?: string
    }

export interface GameStorage {
  load(): LoadResult
  save(snapshot: GameSnapshot): SaveResult
}

function isRecord(value: unknown): value is SnapshotRecord {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function hasOwn(record: SnapshotRecord, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(record, key)
}

function hasSnapshotFields(snapshot: SnapshotRecord): boolean {
  return (
    hasOwn(snapshot, 'resources') &&
    hasOwn(snapshot, 'weaponPower') &&
    hasOwn(snapshot, 'playerMaxHp')
  )
}

function normalizeResources(value: unknown): number {
  if (typeof value === 'number' && Number.isSafeInteger(value) && value >= 0) {
    return Math.min(value, RESOURCE_CAP)
  }

  return 0
}

function normalizeWeaponPower(value: unknown): number {
  if (typeof value === 'number' && Number.isSafeInteger(value) && value >= 1) {
    return value
  }

  return DEFAULT_WEAPON_POWER
}

function normalizePlayerMaxHp(value: unknown): number {
  if (typeof value === 'number' && Number.isSafeInteger(value) && value >= 1) {
    return value
  }

  return DEFAULT_PLAYER_MAX_HP
}

function migrateLegacySnapshot(snapshot: SnapshotRecord): LoadResult {
  if (!hasSnapshotFields(snapshot)) {
    return invalidSchemaResult()
  }

  return loadedSnapshotResult({
    schemaVersion: gameSnapshotSchemaVersion,
    resources: normalizeResources(snapshot.resources),
    weaponPower: normalizeWeaponPower(snapshot.weaponPower),
    playerMaxHp: normalizePlayerMaxHp(snapshot.playerMaxHp),
  })
}

function parseVersionedSnapshot(snapshot: SnapshotRecord): LoadResult {
  if (!hasSnapshotFields(snapshot)) {
    return invalidSchemaResult()
  }

  return loadedSnapshotResult({
    schemaVersion: gameSnapshotSchemaVersion,
    resources: normalizeResources(snapshot.resources),
    weaponPower: normalizeWeaponPower(snapshot.weaponPower),
    playerMaxHp: normalizePlayerMaxHp(snapshot.playerMaxHp),
  })
}

function loadedSnapshotResult(snapshot: GameSnapshot): LoadResult {
  return { ok: true, snapshot, reason: 'loaded' }
}

function emptySnapshotResult(): LoadResult {
  return { ok: true, snapshot: null, reason: 'empty' }
}

function invalidSchemaResult(): LoadResult {
  return { ok: false, snapshot: null, reason: 'invalid-schema' }
}

function loadFailureResult(
  reason: LoadFailureReason,
  error?: unknown,
): LoadResult {
  return {
    ok: false,
    snapshot: null,
    reason,
    errorName: getErrorName(error),
  }
}

function saveFailureResult(
  reason: SaveFailureReason,
  error?: unknown,
): SaveResult {
  return {
    ok: false,
    reason,
    errorName: getErrorName(error),
  }
}

function getErrorName(error: unknown): string | undefined {
  return error instanceof Error ? error.name : undefined
}

function getDefaultStorage(): StorageAdapter | null {
  try {
    return globalThis.localStorage ?? null
  } catch {
    return null
  }
}

function probeStorageAvailability(
  storage: StorageAdapter | null,
  storageKey: string,
): { ok: true; storage: StorageAdapter } | { ok: false; error?: unknown } {
  if (!storage) {
    return { ok: false }
  }

  const probeKey = `${storageKey}.__probe__`

  try {
    storage.setItem(probeKey, '1')
    storage.removeItem(probeKey)
    return { ok: true, storage }
  } catch (error) {
    return { ok: false, error }
  }
}

export function createLocalGameStorage(
  storageKey = defaultSaveKey,
  storage: StorageAdapter | null = getDefaultStorage(),
): GameStorage {
  const availability = probeStorageAvailability(storage, storageKey)

  return {
    load() {
      if (!availability.ok) {
        return loadFailureResult('storage-unavailable', availability.error)
      }

      try {
        const raw = availability.storage.getItem(storageKey)
        return parseSnapshot(raw)
      } catch (error) {
        return loadFailureResult('read-error', error)
      }
    },
    save(snapshot) {
      if (!availability.ok) {
        return saveFailureResult('storage-unavailable', availability.error)
      }

      try {
        availability.storage.setItem(storageKey, serializeSnapshot(snapshot))
        return { ok: true, reason: 'saved' }
      } catch (error) {
        return saveFailureResult('write-error', error)
      }
    },
  }
}

export function serializeSnapshot(snapshot: GameSnapshot): string {
  return JSON.stringify(snapshot)
}

export function parseSnapshot(raw: string | null): LoadResult {
  if (!raw) {
    return emptySnapshotResult()
  }

  try {
    const parsed: unknown = JSON.parse(raw)
    if (!isRecord(parsed)) {
      return invalidSchemaResult()
    }

    if (!hasOwn(parsed, 'schemaVersion')) {
      return migrateLegacySnapshot(parsed)
    }

    if (parsed.schemaVersion !== gameSnapshotSchemaVersion) {
      return loadFailureResult('unsupported-schema')
    }

    return parseVersionedSnapshot(parsed)
  } catch (error) {
    return loadFailureResult('corrupt-json', error)
  }
}
