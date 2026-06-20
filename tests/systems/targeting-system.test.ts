import { describe, expect, it } from 'vitest'

import { selectTarget, type ScoredTarget, type TargetSelectionInput } from '../../src/systems/TargetingSystem'

function makeInput(overrides: Partial<TargetSelectionInput> = {}): TargetSelectionInput {
  return {
    actor: {
      targetEntityId: 'enemy-001',
      faction: 'enemy',
      x: 200,
      y: 120,
      targetingPolicy: 'focus_player',
      ...overrides.actor,
    },
    candidates: overrides.candidates ?? [],
    player: {
      targetEntityId: 'player-alpha',
      x: 240,
      y: 270,
      ...overrides.player,
    },
    arena: {
      width: 960,
      height: 540,
      ...overrides.arena,
    },
    commandIntent: overrides.commandIntent ?? 'none',
    commandIntentActive: overrides.commandIntentActive ?? false,
    previousTargetId: overrides.previousTargetId ?? null,
    threatMode: overrides.threatMode ?? 'binary_hostile_near_player',
    nearPlayerRadiusPx: overrides.nearPlayerRadiusPx ?? 60,
  }
}

const baseCandidates = [
  {
    targetEntityId: '1',
    faction: 'ally' as const,
    x: 400,
    y: 140,
    defeated: false,
    destroyed: false,
    isPlayer: false,
  },
  {
    targetEntityId: '2',
    faction: 'enemy' as const,
    x: 220,
    y: 290,
    defeated: false,
    destroyed: false,
    isPlayer: true,
  },
  {
    targetEntityId: '3',
    faction: 'neutral' as const,
    x: 300,
    y: 260,
    defeated: true,
    destroyed: false,
    isPlayer: false,
  },
]

describe('selectTarget', () => {
  it('GIVEN AC1 candidate filters WHEN defeated / same-faction / outside-arena candidates exist THEN only valid targets remain', () => {
    const input = makeInput({
      actor: {
        faction: 'neutral',
      },
      candidates: [
        ...baseCandidates,
        {
          targetEntityId: 'outside',
          faction: 'enemy',
          x: 1200,
          y: 120,
          defeated: false,
          destroyed: false,
          isPlayer: false,
        },
        {
          targetEntityId: 'same-faction-ally',
          faction: 'neutral',
          x: 200,
          y: 200,
          defeated: false,
          destroyed: false,
          isPlayer: false,
        },
      ],
    })

    const result = selectTarget(input)

    expect(result.selectedTargetId).toBe('2')
    expect(result.scoredCandidates).toHaveLength(2)
    expect(result.scoredCandidates.map((c) => c.targetEntityId)).toEqual(['2', '1'])
  })

  it('GIVEN AC1 stale previous target WHEN that id is not in valid targets THEN clearedStaleTargetId is returned', () => {
    const input = makeInput({
      previousTargetId: 'missing-id',
      candidates: [
        {
          targetEntityId: '2',
          faction: 'neutral',
          x: 240,
          y: 300,
          defeated: false,
          destroyed: false,
          isPlayer: false,
        },
      ],
    })

    const result = selectTarget(input)

    expect(result.clearedStaleTargetId).toBe('missing-id')
    expect(result.selectedTargetId).toBe('2')
  })

  it('GIVEN AC2 tie-break policy WHEN score tuples are equal for two candidates THEN targetEntityId ASC decides deterministically', () => {
    const input = makeInput({
      actor: {
        targetingPolicy: 'focus_player',
      },
      candidates: [
        {
          targetEntityId: 'enemy-b',
          faction: 'neutral',
          x: 240,
          y: 280,
          defeated: false,
          destroyed: false,
          isPlayer: false,
        },
        {
          targetEntityId: 'enemy-a',
          faction: 'neutral',
          x: 240,
          y: 280,
          defeated: false,
          destroyed: false,
          isPlayer: false,
        },
      ],
      previousTargetId: null,
    })

    const first = selectTarget(input)
    const second = selectTarget(input)
    expect(first.selectedTargetId).toBe('enemy-a')
    expect(second.selectedTargetId).toBe('enemy-a')
    expect(first.selectedTargetId).toBe(second.selectedTargetId)
  })

  it('GIVEN AC3 assist_player tuple WHEN commandIntent applies globally THEN commandIntentMatch is a deterministic scalar', () => {
    const input = makeInput({
      actor: {
        targetingPolicy: 'assist_player_threat',
        x: 240,
        y: 260,
        faction: 'ally',
      },
      commandIntent: 'assist_player',
      commandIntentActive: true,
      candidates: [
        {
          targetEntityId: 'far',
          faction: 'neutral',
          x: 700,
          y: 500,
          defeated: false,
          destroyed: false,
          isPlayer: false,
        },
        {
          targetEntityId: 'near',
          faction: 'enemy',
          x: 245,
          y: 265,
          defeated: false,
          destroyed: false,
          isPlayer: false,
        },
      ],
      nearPlayerRadiusPx: 25,
    })

    const inactive = selectTarget({
      ...input,
      commandIntent: 'none',
      commandIntentActive: false,
    })
    const active = selectTarget(input)

    expect(active.scoredCandidates.every((target) => target.commandIntentMatch === 1)).toBe(true)
    expect(inactive.scoredCandidates.every((target) => target.commandIntentMatch === 0)).toBe(true)
    expect(active.selectedTargetId).toBe('near')
  })

  it('GIVEN AC3 assist_player tuple WHEN commandIntentMatch is equal THEN threatToPlayer dominates distance', () => {
    const input = makeInput({
      actor: {
        targetingPolicy: 'assist_player_threat',
        x: 240,
        y: 260,
        faction: 'ally',
      },
      commandIntent: 'assist_player',
      commandIntentActive: true,
      candidates: [
        {
          targetEntityId: 'high-threat',
          faction: 'enemy',
          x: 240,
          y: 260,
          defeated: false,
          destroyed: false,
          isPlayer: false,
        },
        {
          targetEntityId: 'near-low-threat',
          faction: 'enemy',
          x: 300,
          y: 260,
          defeated: false,
          destroyed: false,
          isPlayer: false,
        },
      ],
      nearPlayerRadiusPx: 20,
    })

    const result = selectTarget(input)

    expect(result.selectedTargetId).toBe('high-threat')
    const highThreat = result.scoredCandidates.find((target) => target.targetEntityId === 'high-threat') as ScoredTarget
    const nearThreat = result.scoredCandidates.find((target) => target.targetEntityId === 'near-low-threat') as ScoredTarget
    expect(highThreat.threatToPlayer).toBe(1)
    expect(nearThreat.threatToPlayer).toBe(0)
  })

  it('GIVEN AC3 enemy_chaser tuple WHEN isPlayer flag differs THEN player candidate is preferred even when distance is larger', () => {
    const input = makeInput({
      actor: {
        targetingPolicy: 'focus_player',
        faction: 'ally',
      },
      candidates: [
        {
          targetEntityId: 'near-enemy',
          faction: 'neutral',
          x: 250,
          y: 265,
          defeated: false,
          destroyed: false,
          isPlayer: false,
        },
        {
          targetEntityId: 'far-player',
          faction: 'neutral',
          x: 900,
          y: 300,
          defeated: false,
          destroyed: false,
          isPlayer: true,
        },
      ],
      previousTargetId: 'far-player',
    })

    const result = selectTarget(input)

    expect(result.selectedTargetId).toBe('far-player')
  })

  it('GIVEN AC4 threatToPlayer binary mode WHEN candidate is exactly on boundary THEN threat=1', () => {
    const input = makeInput({
      actor: {
        targetingPolicy: 'assist_player_threat',
        faction: 'ally',
      },
      commandIntent: 'assist_player',
      commandIntentActive: true,
      candidates: [
        {
          targetEntityId: 'inside',
          faction: 'enemy',
          x: 301,
          y: 270,
          defeated: false,
          destroyed: false,
          isPlayer: false,
        },
        {
          targetEntityId: 'boundary',
          faction: 'enemy',
          x: 300,
          y: 270,
          defeated: false,
          destroyed: false,
          isPlayer: false,
        },
      ],
      nearPlayerRadiusPx: 60,
    })

    const result = selectTarget(input)

    const boundary = result.scoredCandidates.find((target) => target.targetEntityId === 'boundary') as ScoredTarget
    expect(boundary.threatToPlayer).toBe(1)
    expect(result.selectedTargetId).toBe('boundary')
  })

  it('GIVEN AC5 deterministic behavior WHEN candidates are reversed between calls THEN selectedTargetId remains stable', () => {
    const input = makeInput({
      actor: {
        targetingPolicy: 'focus_player',
      },
      candidates: [
        { targetEntityId: 'e3', faction: 'enemy', x: 242, y: 271, defeated: false, destroyed: false, isPlayer: false },
        { targetEntityId: 'a1', faction: 'enemy', x: 241, y: 272, defeated: false, destroyed: false, isPlayer: false },
        { targetEntityId: 'c2', faction: 'enemy', x: 240, y: 273, defeated: false, destroyed: false, isPlayer: true },
      ],
      commandIntent: 'assist_player',
      commandIntentActive: true,
    })

    const reversed = makeInput({
      ...input,
      candidates: [...input.candidates].reverse(),
      actor: input.actor,
      commandIntent: input.commandIntent,
      commandIntentActive: input.commandIntentActive,
    })

    const first = selectTarget(input)
    const second = selectTarget(reversed)

    expect(first.selectedTargetId).toBe(second.selectedTargetId)
  })

  it('GIVEN AC5 immutability WHEN selectTarget executes THEN input candidates and input objects remain unchanged', () => {
    const input = makeInput({
      candidates: [
        { targetEntityId: 'x', faction: 'ally', x: 10, y: 20, defeated: false, destroyed: false, isPlayer: false },
        { targetEntityId: 'y', faction: 'neutral', x: 30, y: 40, defeated: false, destroyed: false, isPlayer: true },
      ],
      actor: { x: 12, y: 21 },
      previousTargetId: 'missing',
      commandIntent: 'assist_player',
      commandIntentActive: true,
    })

    const before = structuredClone(input)
    const result = selectTarget(input)

    expect(input).toEqual(before)
    expect(result.scoredCandidates.every((target) => Number.isFinite(target.distanceToPlayer))).toBe(true)
  })
})
