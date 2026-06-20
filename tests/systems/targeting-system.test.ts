import { describe, expect, it } from 'vitest'

import {
  selectTarget,
  type ScoredTarget,
  type TargetSelectionInput,
} from '../../src/systems/TargetingSystem'

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
    isPlayer: false,
  },
  {
    targetEntityId: '3',
    faction: 'neutral' as const,
    x: 300,
    y: 260,
    defeated: false,
    destroyed: false,
    isPlayer: false,
  },
]

describe('selectTarget', () => {
  it('GIVEN AC1 candidate filters WHEN defeated / same-faction / same-policy / neutral / outside-arena candidates exist THEN only valid hostile targets remain', () => {
    const input = makeInput({
      actor: {
        faction: 'ally',
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
          faction: 'ally',
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
    expect(result.scoredCandidates).toHaveLength(1)
    expect(result.scoredCandidates.map((c) => c.targetEntityId)).toEqual(['2'])
  })

  it('GIVEN ignore policy WHEN candidates exist THEN no selection is made', () => {
    const input = makeInput({
      actor: {
        targetingPolicy: 'ignore',
      },
      candidates: [
        ...baseCandidates,
        {
          targetEntityId: 'extra-hostile',
          faction: 'enemy',
          x: 210,
          y: 275,
          defeated: false,
          destroyed: false,
          isPlayer: false,
        },
      ],
    })

    const result = selectTarget(input)

    expect(result.selectedTargetId).toBeNull()
    expect(result.scoredCandidates).toHaveLength(0)
    expect(result.clearedStaleTargetId).toBeNull()
  })

  it('GIVEN nearest_hostile policy WHEN candidates vary in distanceToAlly THEN closest hostile to actor is selected', () => {
    const input = makeInput({
      actor: {
        targetingPolicy: 'nearest_hostile',
        faction: 'ally',
      },
      candidates: [
        {
          targetEntityId: 'far-hostile',
          faction: 'enemy',
          x: 280,
          y: 275,
          defeated: false,
          destroyed: false,
          isPlayer: false,
        },
        {
          targetEntityId: 'near-hostile',
          faction: 'enemy',
          x: 210,
          y: 180,
          defeated: false,
          destroyed: false,
          isPlayer: false,
        },
      ],
    })

    const result = selectTarget(input)

    expect(result.selectedTargetId).toBe('near-hostile')
    expect(result.scoredCandidates.map((target) => target.targetEntityId)).toEqual([
      'near-hostile',
      'far-hostile',
    ])
  })

  it('GIVEN AC2 stale previous target WHEN that id is not in valid targets THEN clearedStaleTargetId is returned', () => {
    const input = makeInput({
      actor: {
        faction: 'ally',
      },
      previousTargetId: 'missing-id',
      candidates: [
        {
          targetEntityId: '2',
          faction: 'enemy',
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
        faction: 'ally',
      },
      candidates: [
        {
          targetEntityId: 'enemy-b',
          faction: 'enemy',
          x: 240,
          y: 280,
          defeated: false,
          destroyed: false,
          isPlayer: false,
        },
        {
          targetEntityId: 'enemy-a',
          faction: 'enemy',
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
          faction: 'enemy',
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
      player: {
        targetEntityId: 'far-player',
      },
      candidates: [
        {
          targetEntityId: 'near-enemy',
          faction: 'enemy',
          x: 250,
          y: 265,
          defeated: false,
          destroyed: false,
          isPlayer: false,
        },
        {
          targetEntityId: 'far-player',
          faction: 'enemy',
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

  it('GIVEN focus_player policy WHEN candidate isPlayer is spoofed THEN player targetEntityId is still preferred', () => {
    const input = makeInput({
      actor: {
        targetingPolicy: 'focus_player',
        faction: 'ally',
      },
      player: {
        targetEntityId: 'actual-player',
        x: 250,
        y: 250,
      },
      candidates: [
        {
          targetEntityId: 'actual-player',
          faction: 'enemy',
          x: 245,
          y: 265,
          defeated: false,
          destroyed: false,
          isPlayer: false,
        },
        {
          targetEntityId: 'spoofed-player',
          faction: 'enemy',
          x: 100,
          y: 100,
          defeated: false,
          destroyed: false,
          isPlayer: true,
        },
      ],
    })

    const result = selectTarget(input)

    expect(result.selectedTargetId).toBe('actual-player')
    const spoofed = result.scoredCandidates.find((target) => target.targetEntityId === 'spoofed-player') as ScoredTarget
    const actual = result.scoredCandidates.find((target) => target.targetEntityId === 'actual-player') as ScoredTarget
    expect(actual.isPlayer).toBe(1)
    expect(spoofed.isPlayer).toBe(0)
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

  it('GIVEN threat radius validation WHEN nearPlayerRadiusPx is negative OR invalid THEN threatToPlayer remains 0', () => {
    const negativeRadius = selectTarget(
      makeInput({
        actor: {
          targetingPolicy: 'assist_player_threat',
          faction: 'ally',
        },
        commandIntent: 'assist_player',
        commandIntentActive: true,
        candidates: [
          {
            targetEntityId: 'near',
            faction: 'enemy',
            x: 300,
            y: 270,
            defeated: false,
            destroyed: false,
            isPlayer: false,
          },
        ],
        nearPlayerRadiusPx: -60,
      }),
    )
    const invalidRadius = selectTarget(
      makeInput({
        actor: {
          targetingPolicy: 'assist_player_threat',
          faction: 'ally',
        },
        commandIntent: 'assist_player',
        commandIntentActive: true,
        candidates: [
          {
            targetEntityId: 'near',
            faction: 'enemy',
            x: 300,
            y: 270,
            defeated: false,
            destroyed: false,
            isPlayer: false,
          },
        ],
        nearPlayerRadiusPx: Number.NaN,
      }),
    )

    expect(negativeRadius.scoredCandidates[0].threatToPlayer).toBe(0)
    expect(invalidRadius.scoredCandidates[0].threatToPlayer).toBe(0)
  })

  it('GIVEN duplicate candidate ids WHEN input includes repeated targetEntityId THEN selectTarget throws', () => {
    const input = makeInput({
      candidates: [
        {
          targetEntityId: 'dup-id',
          faction: 'enemy',
          x: 240,
          y: 260,
          defeated: false,
          destroyed: false,
          isPlayer: false,
        },
        {
          targetEntityId: 'dup-id',
          faction: 'enemy',
          x: 241,
          y: 261,
          defeated: false,
          destroyed: false,
          isPlayer: true,
        },
      ],
    })

    expect(() => selectTarget(input)).toThrowError(/duplicate targetEntityId: dup-id/)
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
        { targetEntityId: 'x', faction: 'enemy', x: 10, y: 20, defeated: false, destroyed: false, isPlayer: false },
        { targetEntityId: 'y', faction: 'enemy', x: 30, y: 40, defeated: false, destroyed: false, isPlayer: true },
      ],
      actor: { x: 12, y: 21, faction: 'ally' },
      player: { targetEntityId: 'y', x: 30, y: 40 },
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
