import type { GameSnapshot } from '../state'

export const defaultSaveKey = 'loop-protocol.mvp.save'

export interface GameStorage {
  load(): GameSnapshot | null
  save(snapshot: GameSnapshot): void
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
    const parsed = JSON.parse(raw) as Partial<GameSnapshot>
    if (
      typeof parsed.resources !== 'number' ||
      typeof parsed.weaponPower !== 'number' ||
      typeof parsed.playerMaxHp !== 'number'
    ) {
      return null
    }

    return {
      resources: parsed.resources,
      weaponPower: parsed.weaponPower,
      playerMaxHp: parsed.playerMaxHp,
    }
  } catch {
    return null
  }
}
