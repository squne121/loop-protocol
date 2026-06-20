import type {
  AllyState,
  CommandIntent,
  EnemyState,
  Faction,
  PlayerState,
  TargetEntityId,
  TargetingPolicy,
} from '../state'

export type TargetEntitySnapshot = Readonly<{
  targetEntityId: TargetEntityId
  faction: Faction
  x: number
  y: number
  defeated?: boolean
  destroyed?: boolean
  isPlayer?: boolean
}>

export type TargetingActorSnapshot = Readonly<{
  targetEntityId: TargetEntityId
  faction: Faction
  x: number
  y: number
  targetingPolicy: TargetingPolicy
}>

export type TargetSelectionInput = Readonly<{
  actor: TargetingActorSnapshot
  candidates: readonly TargetEntitySnapshot[]
  player: Readonly<{
    targetEntityId: TargetEntityId
    x: number
    y: number
  }>
  arena: Readonly<{
    width: number
    height: number
  }>
  commandIntent: CommandIntent
  commandIntentActive: boolean
  previousTargetId: string | null
  threatMode: 'binary_hostile_near_player'
  nearPlayerRadiusPx: number
}>

export type ScoredTarget = Readonly<{
  targetEntityId: TargetEntityId
  commandIntentMatch: 0 | 1
  threatToPlayer: number
  distanceToPlayer: number
  distanceToAlly: number
  isPlayer: 0 | 1
}>

export type TargetSelectionResult = Readonly<{
  selectedTargetId: TargetEntityId | null
  clearedStaleTargetId: string | null
  scoredCandidates: readonly ScoredTarget[]
}>

export function enemyTargetEntityId(enemy: Pick<EnemyState, 'id'>): TargetEntityId {
  return `enemy:${enemy.id}`
}

export function allyTargetEntityId(ally: Pick<AllyState, 'id'>): TargetEntityId {
  return `ally:${ally.id}`
}

export function playerTargetEntityId(player: Pick<PlayerState, 'id'>): TargetEntityId {
  return player.id
}

const ZERO = 0
const ONE = 1

function isFiniteCoord(value: number): boolean {
  return Number.isFinite(value)
}

function isFiniteRectValue(width: number, height: number): boolean {
  return isFiniteCoord(width) && isFiniteCoord(height) && width >= 0 && height >= 0
}

function isInArena(x: number, y: number, width: number, height: number): boolean {
  if (!isFiniteRectValue(width, height)) {
    return false
  }
  return isFiniteCoord(x) && isFiniteCoord(y) && x >= 0 && x <= width && y >= 0 && y <= height
}

function compareNumber(a: number, b: number, direction: 'asc' | 'desc'): number {
  const first = direction === 'asc' ? a - b : b - a
  if (Number.isNaN(first)) {
    return 0
  }
  if (first < 0) return -1
  if (first > 0) return 1
  return 0
}

function compareTargetId(a: TargetEntityId, b: TargetEntityId): number {
  if (a < b) return -1
  if (a > b) return 1
  return 0
}

function isHostile(actorFaction: Faction, candidateFaction: Faction): boolean {
  if (actorFaction === 'neutral' || candidateFaction === 'neutral') return false
  if (actorFaction === 'player' || actorFaction === 'ally') {
    return candidateFaction === 'enemy'
  }
  return candidateFaction === 'ally' || candidateFaction === 'player'
}

function validateCandidateIds(candidates: readonly TargetEntitySnapshot[]): void {
  const seen = new Set<TargetEntityId>()
  for (const candidate of candidates) {
    if (seen.has(candidate.targetEntityId)) {
      throw new Error(`duplicate targetEntityId: ${candidate.targetEntityId}`)
    }
    seen.add(candidate.targetEntityId)
  }
}

function computeThreatSqRadius(input: TargetSelectionInput): number | null {
  return Number.isFinite(input.nearPlayerRadiusPx) && input.nearPlayerRadiusPx >= ZERO
    ? input.nearPlayerRadiusPx * input.nearPlayerRadiusPx
    : null
}

function scoreCandidate(
  input: TargetSelectionInput,
  candidate: TargetEntitySnapshot,
): Omit<ScoredTarget, 'targetEntityId'> {
  const commandIntentMatch: 0 | 1 =
    input.commandIntent === 'assist_player' && input.commandIntentActive ? ONE : ZERO

  const dxToPlayer = candidate.x - input.player.x
  const dyToPlayer = candidate.y - input.player.y
  const distanceToPlayer = Math.hypot(dxToPlayer, dyToPlayer)

  const dxToAlly = candidate.x - input.actor.x
  const dyToAlly = candidate.y - input.actor.y
  const distanceToAlly = Math.hypot(dxToAlly, dyToAlly)

  const threatRadiusSq = computeThreatSqRadius(input)
  const distanceToPlayerSq = dxToPlayer * dxToPlayer + dyToPlayer * dyToPlayer

  const threatToPlayer =
    threatRadiusSq !== null &&
    input.threatMode === 'binary_hostile_near_player' &&
    candidate.faction === 'enemy' &&
    distanceToPlayerSq <= threatRadiusSq
      ? ONE
      : ZERO

  const isPlayer: 0 | 1 = candidate.targetEntityId === input.player.targetEntityId ? ONE : ZERO

  return {
    commandIntentMatch,
    threatToPlayer,
    distanceToPlayer,
    distanceToAlly,
    isPlayer,
  }
}

function compareScoredTargets(a: ScoredTargetWithIndex, b: ScoredTargetWithIndex, policy: TargetingPolicy): number {
  if (policy === 'assist_player_threat') {
    const cmpCommandIntent = compareNumber(a.commandIntentMatch, b.commandIntentMatch, 'desc')
    if (cmpCommandIntent !== 0) return cmpCommandIntent

    const cmpThreat = compareNumber(a.threatToPlayer, b.threatToPlayer, 'desc')
    if (cmpThreat !== 0) return cmpThreat

    const cmpDistanceToPlayer = compareNumber(a.distanceToPlayer, b.distanceToPlayer, 'asc')
    if (cmpDistanceToPlayer !== 0) return cmpDistanceToPlayer

    const cmpDistanceToAlly = compareNumber(a.distanceToAlly, b.distanceToAlly, 'asc')
    if (cmpDistanceToAlly !== 0) return cmpDistanceToAlly
  }

  if (policy === 'focus_player') {
    const cmpIsPlayer = compareNumber(a.isPlayer, b.isPlayer, 'desc')
    if (cmpIsPlayer !== 0) return cmpIsPlayer

    const cmpDistanceToPlayer = compareNumber(a.distanceToPlayer, b.distanceToPlayer, 'asc')
    if (cmpDistanceToPlayer !== 0) return cmpDistanceToPlayer
  }

  if (policy === 'nearest_hostile') {
    const cmpDistanceToAlly = compareNumber(a.distanceToAlly, b.distanceToAlly, 'asc')
    if (cmpDistanceToAlly !== 0) return cmpDistanceToAlly
  }

  const cmpTargetId = compareTargetId(a.targetEntityId, b.targetEntityId)
  if (cmpTargetId !== 0) return cmpTargetId

  return 0
}

type ScoredTargetWithIndex = ScoredTarget

function isValidCandidate(
  input: TargetSelectionInput,
  candidate: TargetEntitySnapshot,
): candidate is TargetEntitySnapshot {
  if (candidate.defeated || candidate.destroyed) return false
  if (!isHostile(input.actor.faction, candidate.faction)) return false
  if (!isFiniteRectValue(input.arena.width, input.arena.height)) return false
  if (!isFiniteCoord(input.actor.x) || !isFiniteCoord(input.actor.y)) return false
  if (!isFiniteCoord(input.player.x) || !isFiniteCoord(input.player.y)) return false
  return isInArena(candidate.x, candidate.y, input.arena.width, input.arena.height)
}

export function selectTarget(input: TargetSelectionInput): TargetSelectionResult {
  validateCandidateIds(input.candidates)

  const validTargets = input.candidates
    .filter((candidate) => isValidCandidate(input, candidate))
    .map((candidate) => {
      const scored = scoreCandidate(input, candidate)
      return {
        targetEntityId: candidate.targetEntityId,
        commandIntentMatch: scored.commandIntentMatch,
        threatToPlayer: scored.threatToPlayer,
        distanceToPlayer: scored.distanceToPlayer,
        distanceToAlly: scored.distanceToAlly,
        isPlayer: scored.isPlayer,
      } satisfies ScoredTargetWithIndex
    })

  const clearedStaleTargetId =
    input.previousTargetId !== null && validTargets.every((target) => target.targetEntityId !== input.previousTargetId)
      ? input.previousTargetId
      : null

  if (input.actor.targetingPolicy === 'ignore') {
    return {
      selectedTargetId: null,
      clearedStaleTargetId,
      scoredCandidates: [],
    }
  }

  const sorted = validTargets
    .slice()
    .sort((a, b) => compareScoredTargets(a, b, input.actor.targetingPolicy))

  const selected = sorted.at(0)

  return {
    selectedTargetId: selected ? selected.targetEntityId : null,
    clearedStaleTargetId,
    scoredCandidates: sorted,
  }
}
