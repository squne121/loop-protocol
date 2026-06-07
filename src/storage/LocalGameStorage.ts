import { gameSnapshotSchemaVersion, type GameSnapshot } from '../state'

export const defaultSaveKey = 'loop-protocol.mvp.save'

const RESOURCE_CAP = 9_999_999
const DEFAULT_WEAPON_POWER = 1
const DEFAULT_PLAYER_MAX_HP = 8

type SnapshotRecord = Record<string, unknown>

export interface GameStorage {
  load(): GameSnapshot | null
  save(snapshot: GameSnapshot): void
}

function isRecord(value: unknown): value is SnapshotRecord {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function hasOwn(record: SnapshotRecord, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(record, key)
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

function migrateLegacySnapshot(snapshot: SnapshotRecord): GameSnapshot {
  return {
    schemaVersion: gameSnapshotSchemaVersion,
    resources: normalizeResources(snapshot.resources),
    weaponPower: normalizeWeaponPower(snapshot.weaponPower),
    playerMaxHp: normalizePlayerMaxHp(snapshot.playerMaxHp),
  }
}

function parseVersionedSnapshot(snapshot: SnapshotRecord): GameSnapshot | null {
  if (!hasOwn(snapshot, 'resources') || !hasOwn(snapshot, 'weaponPower') || !hasOwn(snapshot, 'playerMaxHp')) {
    return null
  }

  return {
    schemaVersion: gameSnapshotSchemaVersion,
    resources: normalizeResources(snapshot.resources),
    weaponPower: normalizeWeaponPower(snapshot.weaponPower),
    playerMaxHp: normalizePlayerMaxHp(snapshot.playerMaxHp),
  }
}

export function createLocalGameStorage(
  storageKey = defaultSaveKey,
  storage: Pick<Storage, 'getItem' | 'setItem'> | null = globalThis.localStorage ??
    null,
): GameStorage {
  return {
    load() {
      if (!storage) {
        return null
      }

      const raw = storage.getItem(storageKey)
      return parseSnapshot(raw)
    },
    save(snapshot) {
      if (!storage) {
        return
      }

      storage.setItem(storageKey, serializeSnapshot(snapshot))
    },
  }
}

export function serializeSnapshot(snapshot: GameSnapshot): string {
  return JSON.stringify(snapshot)
}

export function parseSnapshot(raw: string | null): GameSnapshot | null {
  if (!raw) {
    return null
  }

  try {
    const parsed: unknown = JSON.parse(raw)
    if (!isRecord(parsed)) {
      return null
    }

    if (!hasOwn(parsed, 'schemaVersion')) {
      return migrateLegacySnapshot(parsed)
    }

    if (parsed.schemaVersion !== gameSnapshotSchemaVersion) {
      return null
    }

    return parseVersionedSnapshot(parsed)
  } catch {
    return null
  }
}
