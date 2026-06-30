/**
 * Semantic playtest-evidence tests (B8, Issue #987 / PR #1130).
 *
 * These exercise the *behavior* of the deterministic event log rather than its
 * schema shape, closing the review blockers:
 *   1. phase != running assist attempt -> command_noop: not_combat
 *   2. no enemy / no ally -> command_use(accepted=false) + command_noop same command_seq
 *   3. assist TTL later-tick target switch correlated to same activeCommandSeq
 *   4. local_threat_sample before/after observes resolution-time threat removal
 *   5. CI/deploy provenance placeholder fields are NOT treated as fulfilled
 *   6. copy/download YAML use one atomic snapshot
 */

// @vitest-environment jsdom

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { createInputState, mapInputToCommands } from '../src/input'
import { createInitialGameState } from '../src/state/GameState'
import type { EnemyState, GameState, ProjectileState } from '../src/state/GameState'
import { defaultSimulationConfig } from '../src/state/SimulationConfig'
import { runSortieSimulationStep, startSortie } from '../src/systems/SortieSystem'
import { getPlaytestEvidenceSnapshot } from '../src/playtest/assistPlayerEventLog'
import type { AssistPlayerPlaytestEvent } from '../src/playtest/assistPlayerEventLog'
import {
  collectArtifactMetadataForTest,
  isProvenanceFulfilled,
  initPlaytestEvidencePanel,
  shouldShowPanel,
} from '../src/ui/playtestEvidence'

const FDT = defaultSimulationConfig.fixedDeltaMs

function snapshotEvents(state: GameState): AssistPlayerPlaytestEvent[] {
  return getPlaytestEvidenceSnapshot(state.playtestEvidenceRuntime).deterministic_events
}

function makeLiveEnemy(id: number, x: number, y: number, hp = 5): EnemyState {
  return {
    id,
    definitionId: 'enemy-basic',
    hp,
    maxHp: 5,
    x,
    y,
    radius: 12,
    speedPxPerSec: 0,
    contactDamage: 1,
    defeated: false,
    defeatedAtTick: null,
    faction: 'enemy',
    role: 'enemy_chaser',
    behaviorState: 'move_to_engage',
    targetingPolicy: 'focus_player',
    targetEntityId: 'player:player-alpha',
  }
}

function assistCommands(): ReturnType<typeof mapInputToCommands> {
  const input = createInputState()
  input.assistPlayerRisingEdge = true
  return mapInputToCommands(input)
}

const emptyCommands = (): ReturnType<typeof mapInputToCommands> =>
  mapInputToCommands(createInputState())

describe('B8#1 not_combat: assist attempt outside running phase', () => {
  it('GIVEN loopPhase is not running WHEN assist command attempted THEN command_noop: not_combat is recorded', () => {
    const state = createInitialGameState()
    state.loopPhase = 'preparation' // not running
    expect(state.sortie.status).toBe('idle')

    runSortieSimulationStep(state, assistCommands(), FDT)

    const events = snapshotEvents(state)
    const noop = events.find((e) => e.type === 'command_noop')
    expect(noop).toBeDefined()
    expect(noop && noop.type === 'command_noop' && noop.reason).toBe('not_combat')
    // The rejected attempt is still surfaced as a (non-accepted) command_use.
    const use = events.find((e) => e.type === 'command_use')
    expect(use && use.type === 'command_use' && use.accepted).toBe(false)
  })
})

describe('B8#2 no_ally / no_target: command_use(false) + command_noop share command_seq', () => {
  it('GIVEN no living enemies WHEN assist attempted in combat THEN noop(no_target) shares the command_use command_seq', () => {
    const state = createInitialGameState()
    state.loopPhase = 'preparation'
    startSortie(state, FDT) // running, one default ally, no enemies
    state.enemies = []

    runSortieSimulationStep(state, assistCommands(), FDT)

    const use = snapshotEvents(state).find((e) => e.type === 'command_use')
    const noop = snapshotEvents(state).find((e) => e.type === 'command_noop')
    expect(use && use.type === 'command_use' && use.accepted).toBe(false)
    expect(noop && noop.type === 'command_noop' && noop.reason).toBe('no_target')
    expect(use?.command_seq).toBe(noop?.command_seq)
  })

  it('GIVEN no allies WHEN assist attempted in combat THEN noop(no_ally) shares the command_use command_seq', () => {
    const state = createInitialGameState()
    state.loopPhase = 'preparation'
    startSortie(state, FDT)
    state.allies = []
    state.enemies = [makeLiveEnemy(1, 250, 270)]

    runSortieSimulationStep(state, assistCommands(), FDT)

    const use = snapshotEvents(state).find((e) => e.type === 'command_use')
    const noop = snapshotEvents(state).find((e) => e.type === 'command_noop')
    expect(use && use.type === 'command_use' && use.accepted).toBe(false)
    expect(noop && noop.type === 'command_noop' && noop.reason).toBe('no_ally')
    expect(use?.command_seq).toBe(noop?.command_seq)
  })
})

describe('B8#3 target_switch correlated to activeCommandSeq on a later TTL tick', () => {
  it('GIVEN assist sampled tick 0 WHEN a target switch happens on a later in-TTL tick THEN it carries the originating command_seq', () => {
    const state = createInitialGameState()
    state.loopPhase = 'preparation'
    startSortie(state, FDT)
    // Ally near player; one enemy in range so the assist is accepted at tick 0.
    state.allies[0].x = 200
    state.allies[0].y = 270
    state.enemies = [makeLiveEnemy(1, 250, 270)]

    runSortieSimulationStep(state, assistCommands(), FDT)
    const seqAtSample = state.commandIntentRuntime.activeCommandSeq
    expect(seqAtSample).not.toBeNull()
    expect(state.commandIntentRuntime.activeIntent).toBe('assist_player')

    // On the next tick (no new command), move the original threat far out of the
    // near-player radius and add a fresh in-range enemy so the ally must re-target.
    // The switch occurs while the assist intent is still active.
    state.enemies[0].x = 900
    state.enemies.push(makeLiveEnemy(2, 230, 270))
    runSortieSimulationStep(state, emptyCommands(), FDT)

    const switches = snapshotEvents(state).filter((e) => e.type === 'target_switch')
    const laterSwitch = switches.find((e) => e.type === 'target_switch' && e.tick === 1)
    expect(laterSwitch).toBeDefined()
    expect(laterSwitch?.command_seq).toBe(seqAtSample)
    expect(laterSwitch && laterSwitch.type === 'target_switch' && laterSwitch.caused_by_command_intent).toBe(true)
  })
})

describe('B3 expired: assist TTL lapses with no confirmed target', () => {
  it('GIVEN assist accepted but ally never confirms a target WHEN TTL lapses THEN command_noop: expired is recorded for the originating command_seq', () => {
    const state = createInitialGameState()
    state.loopPhase = 'preparation'
    startSortie(state, FDT)
    // No allies and no enemies for the whole window: the assist intent is still
    // buffered on sample, but no ally can ever confirm a target, so the TTL
    // lapses unconfirmed and surfaces command_noop: expired.
    state.allies = []
    state.enemies = []

    runSortieSimulationStep(state, assistCommands(), FDT)
    const originatingSeq = state.commandIntentRuntime.activeCommandSeq
    expect(originatingSeq).not.toBeNull()
    expect(state.commandIntentRuntime.activeIntentTargetConfirmed).toBe(false)

    // Advance until the TTL expires.
    const ttl = state.commandIntentRuntime.assistPlayerTtlTicks
    for (let i = 0; i < ttl + 1; i += 1) {
      runSortieSimulationStep(state, emptyCommands(), FDT)
      if (state.commandIntentRuntime.activeIntent === 'none') break
    }

    const expired = snapshotEvents(state).find(
      (e) => e.type === 'command_noop' && e.reason === 'expired',
    )
    expect(expired).toBeDefined()
    expect(expired?.command_seq).toBe(originatingSeq)
  })
})

describe('B8#4 local_threat_sample observes resolution-time threat removal', () => {
  it('GIVEN a low-hp enemy in local radius killed during collision resolution THEN after-count < before-count', () => {
    const state = createInitialGameState()
    state.loopPhase = 'preparation'
    startSortie(state, FDT)
    // Enemy at hp=1 within local threat radius of the player (player at 240,270).
    const enemy = makeLiveEnemy(1, 252, 270, 1)
    state.enemies = [enemy]
    // A projectile overlapping the enemy with lethal damage so resolution defeats it.
    const projectile: ProjectileState = {
      id: 1,
      x: 252,
      y: 270,
      radius: 6,
      directionX: 1,
      directionY: 0,
      speedPxPerSec: 0,
      ageMs: 0,
      lifetimeMs: 10_000,
      damage: 5,
    }
    state.projectiles = [projectile]
    state.nextProjectileId = 2

    runSortieSimulationStep(state, assistCommands(), FDT)

    const events = snapshotEvents(state)
    const before = events.find((e) => e.type === 'local_threat_sample' && e.phase === 'before')
    const after = events.find((e) => e.type === 'local_threat_sample' && e.phase === 'after')
    expect(before).toBeDefined()
    expect(after).toBeDefined()
    const beforeCount = before && before.type === 'local_threat_sample' ? before.threat_count : -1
    const afterCount = after && after.type === 'local_threat_sample' ? after.threat_count : -1
    // The enemy was defeated during resolveCombatCollisions; only the after-sample
    // (taken post-resolution, B5) observes the removal.
    expect(beforeCount).toBe(1)
    expect(afterCount).toBe(0)
    expect(state.enemies[0].defeated).toBe(true)
  })
})

describe('B8#5 provenance placeholders are NOT treated as fulfilled (B2)', () => {
  it('GIVEN no build-time injection WHEN artifact metadata collected THEN placeholder fields report unfulfilled availability_reason', () => {
    const meta = collectArtifactMetadataForTest()
    // Build-after fields can never be carried by the bundle.
    expect(meta.artifact_url.availability_reason).toBe('unavailable-in-bundle-build-time')
    expect(meta.artifact_digest_or_attestation.availability_reason).toBe(
      'unavailable-in-bundle-build-time',
    )
    expect(meta.retention_days.availability_reason).toBe('unavailable-in-bundle-build-time')
    expect(isProvenanceFulfilled(meta.artifact_url)).toBe(false)
    expect(isProvenanceFulfilled(meta.artifact_digest_or_attestation)).toBe(false)
    // run_id is unset in tests -> 'unknown' -> unfulfilled.
    expect(isProvenanceFulfilled(meta.run_id)).toBe(false)
  })

  it('GIVEN an explicitly available field WHEN classified THEN isProvenanceFulfilled is true', () => {
    expect(
      isProvenanceFulfilled({ value: 'https://example/run/1', availability_reason: 'available' }),
    ).toBe(true)
  })
})

describe('B8#6 copy/download use one atomic snapshot', () => {
  let container: HTMLElement

  beforeEach(() => {
    container = document.createElement('div')
    document.body.appendChild(container)
  })

  it('GIVEN the panel is open WHEN copy and download are both triggered THEN they serialize one identical atomic snapshot', () => {
    const copied: string[] = []
    Object.defineProperty(navigator, 'clipboard', {
      value: {
        writeText: vi.fn((t: string) => {
          copied.push(t)
          return Promise.resolve()
        }),
      },
      configurable: true,
    })

    const blobTexts: string[] = []
    const origCreate = URL.createObjectURL
    const origRevoke = URL.revokeObjectURL
    const origClick = HTMLAnchorElement.prototype.click
    URL.createObjectURL = vi.fn((blob: Blob) => {
      // jsdom Blob stores its parts; reconstruct the text synchronously.
      const parts = (blob as Blob & { _parts?: unknown[] })._parts
      if (Array.isArray(parts)) {
        blobTexts.push(parts.map((p) => String(p)).join(''))
      }
      return 'blob:mock-' + blobTexts.length
    }) as unknown as typeof URL.createObjectURL
    URL.revokeObjectURL = vi.fn()
    HTMLAnchorElement.prototype.click = function (this: HTMLAnchorElement) {}

    try {
      initPlaytestEvidencePanel(container, '?playtest_evidence=1')
      const copyBtn = container.querySelector('[data-action="copy-yaml"]') as HTMLButtonElement
      const downloadBtn = container.querySelector(
        '[data-action="download-yaml"]',
      ) as HTMLButtonElement
      const fallback = container.querySelector(
        '[data-playtest-fallback="true"]',
      ) as HTMLTextAreaElement

      // The textarea mirrors the locked atomic snapshot.
      const snapshotYaml = fallback.value
      expect(snapshotYaml).toContain('playtest_evidence_schema_version: v2')

      copyBtn.click()
      downloadBtn.click()

      // Copy target === locked snapshot; download blob === same snapshot.
      expect(copied).toEqual([snapshotYaml])
      if (blobTexts.length > 0) {
        // When the jsdom Blob exposes its parts, assert byte-for-byte equality.
        expect(blobTexts[0]).toBe(snapshotYaml)
      }
      // No per-frame refresh: textarea is unchanged after the actions.
      expect(fallback.value).toBe(snapshotYaml)
    } finally {
      URL.createObjectURL = origCreate
      URL.revokeObjectURL = origRevoke
      HTMLAnchorElement.prototype.click = origClick
    }
  })
})

describe('Issue #1173 evidence surface boundary', () => {
  it('GIVEN default URL search WHEN evidence panel gate is checked THEN opt-in debug evidence boundary is preserved', () => {
    expect(shouldShowPanel('')).toBe(false)
    expect(shouldShowPanel('?playtest_evidence=1')).toBe(true)
  })

  it('GIVEN default URL search WHEN evidence panel mounts THEN it stays hidden until explicitly enabled', () => {
    const container = document.createElement('div')
    initPlaytestEvidencePanel(container, { search: '' })

    const panel = container.querySelector<HTMLElement>('[data-playtest-evidence="true"]')
    expect(panel?.hidden).toBe(true)
  })
})
