/**
 * @vitest-environment jsdom
 *
 * Issue #1375: `HudController` now composes two DOM roots inside
 * `battle-hud-layer`:
 *
 * - `data-combat-hud` (Hull / Kills / Elapsed / Weapon / Assist / Pause) --
 *   visible only during `running` (`src/ui/combatHud.ts` owns the view
 *   model formatter; `HudController` owns phase routing).
 * - `data-legacy-result-surface` -- the temporary compatibility surface
 *   that keeps `Return to hangar` / `Collect payout` / `Prepare next
 *   sortie` alive until #1376. Visible everywhere EXCEPT `running`.
 *
 * Title / load / preparation moved to `phaseScreens.ts`'s
 * `createPhaseScreenController()` (`battle-screen-layer`, Issue #1374) --
 * this file tests both modules (both live under `tests/hud-controller.test.ts`
 * in Issue #1374/#1375's Allowed Paths; there is no separate phaseScreens
 * unit test file). Real role/name/visibility/focus verification for the
 * phase screens and for the combat HUD lives in
 * `tests/e2e/phase-screens.spec.ts` / `tests/e2e/m2-combat-mvp.spec.ts`
 * (real Playwright).
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createHudController, getUpgradeStatusCopy } from '../src/ui/HudController'
import { buildCombatHudViewModel, getCombatHudAssistStatusCopy } from '../src/ui/combatHud'
import {
  createPhaseScreenController,
  getVisiblePhaseScreen,
  resolveLoadMenuBackIntent,
} from '../src/ui/phaseScreens'
import {
  runNextSortieHandler,
  runConfirmResultHandler,
} from '../src/main'
import type { GameState, LoopPhase, ResultRewardStatus, SortieResult } from '../src/state'
import { createDefaultAllyState, createGameSnapshot } from '../src/state'

/** Production fixed simulation timestep (matches `defaultSimulationConfig.fixedDeltaMs`). */
const PROD_FIXED_DELTA_MS = 1000 / 60

const TERMINAL_SORTIE_RESULT = {
  outcome: 'victory',
  endReason: 'all_enemies_defeated',
  durationMs: 30_000,
  kills: 4,
  shotsFired: 18,
  playerHpRemaining: 6,
} satisfies SortieResult

function createState(loopPhase: LoopPhase = 'preparation', resultRewardStatus: ResultRewardStatus = 'pending'): GameState {
  const isDebrief = loopPhase === 'debrief_pending_reward' || loopPhase === 'debrief_reward_claimed'
  const isResult = loopPhase === 'result'

  return {
    tick: 0,
    elapsedMs: 0,
    loopPhase,
    resultRewardStatus,
    pendingRewardApplicationId: (isDebrief || isResult) ? 'sortie-reward-1' : null,
    nextRewardApplicationSequence: 2,
    arena: { width: 960, height: 540 },
    player: {
      id: 'player-alpha',
      x: 240,
      y: 270,
      radius: 14,
      speed: 210,
      hp: 8,
      maxHp: 8,
      aimX: 540,
      aimY: 270,
      weaponCooldownMs: 0,
      weaponIntervalMs: 280,
      shotsFired: 0,
      lastAimDirectionX: 1,
      lastAimDirectionY: 0,
    },
    enemies: [],
    projectiles: [],
    nextProjectileId: 1,
    nextEnemyId: 1,
    progress: {
      stageLabel: 'MVP Sortie',
      resources: 12,
      weaponPower: 1,
    },
    allies: [],
    nextAllyId: 2,
    rewardClaims: {
      claimedApplicationIds:
        loopPhase === 'debrief_reward_claimed'
          ? ({ 'sortie-reward-1': true } as Record<string, true>)
          : (Object.create(null) as Record<string, true>),
    },
    telemetry: {
      status: 'Combat systems green',
      lastCommandSummary: 'Reset sortie is a destructive boundary. Preparation only.',
    },
    commandIntentRuntime: {
      activeIntent: 'none',
      bufferedIntent: null,
      assistPlayerTtlTicks: 8,
      activeCommandSeq: null,
      activeIntentTargetConfirmed: false,
    },
    sortie:
      loopPhase === 'running'
        ? {
            status: 'running',
            elapsedTicks: 30,
            targetTicks: 1800,
            result: null,
          }
        : (isDebrief || isResult)
          ? {
              status: 'victory',
              elapsedTicks: 1800,
              targetTicks: 1800,
              result: TERMINAL_SORTIE_RESULT,
            }
        : {
            status: 'idle',
            elapsedTicks: 0,
            targetTicks: 1800,
            result: null,
          },
  }
}

function queryButton(container: HTMLElement, action: string): HTMLButtonElement {
  const button = container.querySelector<HTMLButtonElement>(`[data-action="${action}"]`)

  if (!button) {
    throw new Error(`Button ${action} not found`)
  }

  return button
}

function combatHudRoot(container: HTMLElement): HTMLElement {
  const root = container.querySelector<HTMLElement>('[data-combat-hud]')
  if (!root) {
    throw new Error('data-combat-hud root not found')
  }
  return root
}

function legacyResultSurface(container: HTMLElement): HTMLElement {
  const root = container.querySelector<HTMLElement>('[data-legacy-result-surface]')
  if (!root) {
    throw new Error('data-legacy-result-surface root not found')
  }
  return root
}

describe('HudController: data-combat-hud (running only, Issue #1375)', () => {
  let container: HTMLElement
  let actions: {
    onAssistPlayerCommand: ReturnType<typeof vi.fn>
    onClaimReward: ReturnType<typeof vi.fn>
    onConfirmResult: ReturnType<typeof vi.fn>
    onNextSortie: ReturnType<typeof vi.fn>
    onTogglePause: ReturnType<typeof vi.fn>
  }
  let hudController: ReturnType<typeof createHudController>

  beforeEach(() => {
    container = document.createElement('div')
    actions = {
      onAssistPlayerCommand: vi.fn(),
      onClaimReward: vi.fn(),
      onConfirmResult: vi.fn(),
      onNextSortie: vi.fn(),
      onTogglePause: vi.fn(),
    }
    hudController = createHudController(container, actions)
  })

  it('GIVEN running WHEN render called THEN data-combat-hud is visible/focusable and data-legacy-result-surface is hidden/inert (AC1)', () => {
    hudController.render(createState('running'), false, PROD_FIXED_DELTA_MS)

    const combat = combatHudRoot(container)
    const legacy = legacyResultSurface(container)

    expect(combat.hidden).toBe(false)
    expect(combat.hasAttribute('inert')).toBe(false)
    expect(legacy.hidden).toBe(true)
    expect(legacy.hasAttribute('inert')).toBe(true)
  })

  it('GIVEN preparation (or any non-running phase) WHEN render called THEN data-combat-hud is hidden/inert and data-legacy-result-surface is visible (AC1)', () => {
    hudController.render(createState('preparation'), false, PROD_FIXED_DELTA_MS)

    const combat = combatHudRoot(container)
    const legacy = legacyResultSurface(container)

    expect(combat.hidden).toBe(true)
    expect(combat.hasAttribute('inert')).toBe(true)
    expect(legacy.hidden).toBe(false)
    expect(legacy.hasAttribute('inert')).toBe(false)
  })

  it('GIVEN running WHEN render called THEN the combat HUD surface contains only Hull/Kills/Elapsed/Weapon/Assist/Pause, never Mission phase/status/outcome/Pilot updates/raw telemetry/result actions (AC2)', () => {
    hudController.render(createState('running'), false, PROD_FIXED_DELTA_MS)

    const combat = combatHudRoot(container)
    const text = combat.textContent ?? ''

    expect(combat.querySelector('[data-field="combat-hud-hull"]')).not.toBeNull()
    expect(combat.querySelector('[data-field="combat-hud-kills"]')).not.toBeNull()
    expect(combat.querySelector('[data-field="combat-hud-elapsed"]')).not.toBeNull()
    expect(combat.querySelector('[data-field="combat-hud-weapon"]')).not.toBeNull()
    expect(combat.querySelector('[data-action="assist-player"]')).not.toBeNull()
    expect(combat.querySelector('[data-action="toggle-pause"]')).not.toBeNull()

    expect(combat.querySelector('[data-field="loop-phase"]')).toBeNull()
    expect(combat.querySelector('[data-field="sortie-status"]')).toBeNull()
    expect(combat.querySelector('[data-field="sortie-result"]')).toBeNull()
    expect(combat.querySelector('[data-field="status"]')).toBeNull()
    expect(combat.querySelector('[data-field="command"]')).toBeNull()
    expect(combat.querySelector('[data-action="claim-reward"]')).toBeNull()
    expect(combat.querySelector('[data-action="confirm-result"]')).toBeNull()
    expect(combat.querySelector('[data-action="next-sortie"]')).toBeNull()
    expect(text).not.toContain('Collect payout')
    expect(text).not.toContain('Return to hangar')
    expect(text).not.toContain('Prepare next sortie')
  })

  it('GIVEN Hull 6/8 WHEN render called THEN combat-hud-hull shows the formatted Hull value (AC2)', () => {
    const state = createState('running')
    state.player.hp = 6
    state.player.maxHp = 8

    hudController.render(state, false, PROD_FIXED_DELTA_MS)

    expect(combatHudRoot(container).querySelector('[data-field="combat-hud-hull"]')?.textContent).toBe('6/8')
  })

  it('GIVEN 2 defeated enemies WHEN render called THEN combat-hud-kills reflects the live count (AC2)', () => {
    const state = createState('running')
    state.enemies = [
      { id: 1, definitionId: 'enemy-basic', hp: 0, maxHp: 5, x: 0, y: 0, radius: 12, speedPxPerSec: 60, contactDamage: 1, defeated: true, defeatedAtTick: 10, faction: 'enemy', role: 'enemy_chaser', behaviorState: 'move_to_engage', targetingPolicy: 'focus_player', targetEntityId: null },
      { id: 2, definitionId: 'enemy-basic', hp: 5, maxHp: 5, x: 0, y: 0, radius: 12, speedPxPerSec: 60, contactDamage: 1, defeated: false, defeatedAtTick: null, faction: 'enemy', role: 'enemy_chaser', behaviorState: 'move_to_engage', targetingPolicy: 'focus_player', targetEntityId: null },
    ]

    hudController.render(state, false, PROD_FIXED_DELTA_MS)

    expect(combatHudRoot(container).querySelector('[data-field="combat-hud-kills"]')?.textContent).toBe('1')
  })

  it('GIVEN elapsedTicks: 900 and activeFixedDeltaMs: 16 WHEN render called THEN combat-hud-elapsed reads 14.4 s, not /60 or wall-clock derived (AC4)', () => {
    const state = createState('running')
    state.sortie = { status: 'running', elapsedTicks: 900, targetTicks: 3600, result: null }

    hudController.render(state, false, 16)

    const elapsedField = combatHudRoot(container).querySelector('[data-field="combat-hud-elapsed"]')
    expect(elapsedField?.textContent).toBe('14.4 s')
    expect(elapsedField?.hasAttribute('data-visual-mask')).toBe(false)
  })

  it('GIVEN the same view model WHEN render is called twice THEN combat HUD text nodes are not reassigned on the second render (AC6, Issue #1375 PR #1925 review P1-1)', () => {
    const state = createState('running')
    state.sortie = { status: 'running', elapsedTicks: 900, targetTicks: 3600, result: null }

    hudController.render(state, false, 16)

    const observer = new MutationObserver(() => {})
    observer.observe(combatHudRoot(container), {
      characterData: true,
      childList: true,
      subtree: true,
    })

    // Same state object, same isPaused, same activeFixedDeltaMs -> identical
    // view model on the second render.
    hudController.render(state, false, 16)

    // MutationObserver callbacks are microtask-queued; `takeRecords()`
    // synchronously drains the pending queue so this assertion does not
    // depend on the callback having flushed yet.
    const observed = observer.takeRecords().map((mutation) => mutation.type)
    observer.disconnect()
    expect(observed).toEqual([])
  })

  it('GIVEN only the Assist status changes WHEN render is called THEN only combat-hud-assist-status is patched, not Hull/Kills/Elapsed/Weapon (AC6, Issue #1375 PR #1925 review P1-1)', () => {
    const state = createState('running')
    state.allies = [createDefaultAllyState(1)]
    state.enemies = [
      { id: 1, definitionId: 'enemy-basic', hp: 5, maxHp: 5, x: 0, y: 0, radius: 12, speedPxPerSec: 60, contactDamage: 1, defeated: false, defeatedAtTick: null, faction: 'enemy', role: 'enemy_chaser', behaviorState: 'move_to_engage', targetingPolicy: 'focus_player', targetEntityId: null },
    ]

    hudController.render(state, false, PROD_FIXED_DELTA_MS)

    const root = combatHudRoot(container)
    const observer = new MutationObserver(() => {})
    observer.observe(root, { characterData: true, childList: true, subtree: true })

    // Only the assist-relevant state changes; Hull/Kills/Elapsed/Weapon
    // inputs are unchanged.
    state.commandIntentRuntime.activeIntent = 'assist_player'
    hudController.render(state, false, PROD_FIXED_DELTA_MS)

    // MutationObserver callbacks are microtask-queued; `takeRecords()`
    // synchronously drains the pending queue so this assertion does not
    // depend on the callback having flushed yet.
    const mutations = observer.takeRecords()
    observer.disconnect()

    const changedNodes = mutations
      .map((mutation) =>
        mutation.target.nodeType === Node.TEXT_NODE
          ? mutation.target.parentElement
          : (mutation.target as Element),
      )
      .filter((node): node is Element => node !== null)

    const changedFields = new Set(
      changedNodes
        .map((node) => node.closest('[data-field]')?.getAttribute('data-field'))
        .filter((field): field is string => field !== null && field !== undefined),
    )

    expect(changedFields.has('combat-hud-assist-status')).toBe(true)
    expect(changedFields.has('combat-hud-hull')).toBe(false)
    expect(changedFields.has('combat-hud-kills')).toBe(false)
    expect(changedFields.has('combat-hud-elapsed')).toBe(false)
    expect(changedFields.has('combat-hud-weapon')).toBe(false)
  })

  it('GIVEN weaponCooldownMs 0 WHEN render called THEN combat-hud-weapon shows Ready, never the raw millisecond value (AC2)', () => {
    const state = createState('running')
    state.player.weaponCooldownMs = 0

    hudController.render(state, false, PROD_FIXED_DELTA_MS)

    const weaponField = combatHudRoot(container).querySelector('[data-field="combat-hud-weapon"]')
    expect(weaponField?.textContent).toBe('Ready')
  })

  it('GIVEN weaponCooldownMs > 0 WHEN render called THEN combat-hud-weapon shows Recharging, never the raw millisecond value (AC2)', () => {
    const state = createState('running')
    state.player.weaponCooldownMs = 137.4

    hudController.render(state, false, PROD_FIXED_DELTA_MS)

    const weaponField = combatHudRoot(container).querySelector('[data-field="combat-hud-weapon"]')
    expect(weaponField?.textContent).toBe('Recharging')
    expect(container.textContent).not.toContain('137')
  })

  it('GIVEN combat HUD rendered WHEN queried by role THEN Assist allies is a unique button with a fixed accessible name (AC3)', () => {
    hudController.render(createState('running'), false, PROD_FIXED_DELTA_MS)

    const assistButtons = Array.from(container.querySelectorAll('button')).filter(
      (button) => button.textContent?.trim() === 'Assist allies',
    )
    expect(assistButtons).toHaveLength(1)
    expect(assistButtons[0].getAttribute('aria-label')).toBe('Assist allies')
  })

  it('GIVEN Pause toggled WHEN render called THEN the visible label stays "Pause" and aria-pressed represents the toggle state (AC3)', () => {
    hudController.render(createState('running'), false, PROD_FIXED_DELTA_MS)
    const pauseButton = queryButton(container, 'toggle-pause')
    expect(pauseButton.textContent?.trim()).toBe('Pause')
    expect(pauseButton.getAttribute('aria-pressed')).toBe('false')
    // Accessible name (no aria-label override) equals the visible label.
    expect(pauseButton.hasAttribute('aria-label')).toBe(false)

    hudController.render(createState('running'), true, PROD_FIXED_DELTA_MS)
    expect(pauseButton.textContent?.trim()).toBe('Pause')
    expect(pauseButton.getAttribute('aria-pressed')).toBe('true')
    expect(pauseButton.hasAttribute('aria-label')).toBe(false)
  })

  it('GIVEN running with ally and living enemy WHEN render called THEN combat-hud-assist-status reports ready and is the sole polite live region for combat feedback (AC2, AC6)', () => {
    const state = createState('running')
    state.allies = [createDefaultAllyState(1)]
    state.enemies = [
      { id: 1, definitionId: 'enemy-basic', hp: 5, maxHp: 5, x: 360, y: 270, radius: 12, speedPxPerSec: 60, contactDamage: 1, defeated: false, defeatedAtTick: null, faction: 'enemy', role: 'enemy_chaser', behaviorState: 'move_to_engage', targetingPolicy: 'focus_player', targetEntityId: 'player:player-alpha' },
    ]

    hudController.render(state, false, PROD_FIXED_DELTA_MS)

    const assistStatus = combatHudRoot(container).querySelector('[data-field="combat-hud-assist-status"]')
    expect(queryButton(container, 'assist-player').disabled).toBe(false)
    expect(assistStatus?.textContent).toBe('Assist ready.')
    expect(assistStatus?.getAttribute('role')).toBe('status')
    expect(assistStatus?.getAttribute('aria-live')).toBe('polite')
    expect(assistStatus?.getAttribute('aria-atomic')).toBe('true')

    // AC6: Hull/Kills/Elapsed/Weapon are not inside a role="status" region.
    const combat = combatHudRoot(container)
    for (const field of ['combat-hud-hull', 'combat-hud-kills', 'combat-hud-elapsed', 'combat-hud-weapon']) {
      expect(combat.querySelector(`[data-field="${field}"]`)?.getAttribute('role')).toBeNull()
    }
  })

  it('GIVEN running without allies WHEN render called THEN assist status reports no ally available', () => {
    const state = createState('running')
    state.enemies = [
      { id: 1, definitionId: 'enemy-basic', hp: 5, maxHp: 5, x: 360, y: 270, radius: 12, speedPxPerSec: 60, contactDamage: 1, defeated: false, defeatedAtTick: null, faction: 'enemy', role: 'enemy_chaser', behaviorState: 'move_to_engage', targetingPolicy: 'focus_player', targetEntityId: 'player:player-alpha' },
    ]

    hudController.render(state, false, PROD_FIXED_DELTA_MS)

    expect(combatHudRoot(container).querySelector('[data-field="combat-hud-assist-status"]')?.textContent).toBe(
      'No ally available.',
    )
  })

  it('GIVEN running with ally but no valid target WHEN render called THEN assist status reports no target to assist', () => {
    const state = createState('running')
    state.allies = [createDefaultAllyState(1)]

    hudController.render(state, false, PROD_FIXED_DELTA_MS)

    expect(combatHudRoot(container).querySelector('[data-field="combat-hud-assist-status"]')?.textContent).toBe(
      'No target to assist.',
    )
  })

  it('GIVEN active assist without assigned target WHEN render called THEN assist status reports signal sent', () => {
    const state = createState('running')
    state.allies = [createDefaultAllyState(1)]
    state.enemies = [
      { id: 1, definitionId: 'enemy-basic', hp: 5, maxHp: 5, x: 360, y: 270, radius: 12, speedPxPerSec: 60, contactDamage: 1, defeated: false, defeatedAtTick: null, faction: 'enemy', role: 'enemy_chaser', behaviorState: 'move_to_engage', targetingPolicy: 'focus_player', targetEntityId: 'player:player-alpha' },
    ]
    state.commandIntentRuntime.activeIntent = 'assist_player'

    hudController.render(state, false, PROD_FIXED_DELTA_MS)

    expect(combatHudRoot(container).querySelector('[data-field="combat-hud-assist-status"]')?.textContent).toBe(
      'Assist signal sent.',
    )
  })

  it('GIVEN active assist with assigned target WHEN render called THEN assist status reports allies covering you', () => {
    const state = createState('running')
    const ally = createDefaultAllyState(1)
    ally.targetEntityId = 'enemy:1'
    state.allies = [ally]
    state.enemies = [
      { id: 1, definitionId: 'enemy-basic', hp: 5, maxHp: 5, x: 360, y: 270, radius: 12, speedPxPerSec: 60, contactDamage: 1, defeated: false, defeatedAtTick: null, faction: 'enemy', role: 'enemy_chaser', behaviorState: 'move_to_engage', targetingPolicy: 'focus_player', targetEntityId: 'player:player-alpha' },
    ]
    state.commandIntentRuntime.activeIntent = 'assist_player'

    hudController.render(state, false, PROD_FIXED_DELTA_MS)

    expect(combatHudRoot(container).querySelector('[data-field="combat-hud-assist-status"]')?.textContent).toBe(
      'Allies covering you.',
    )
  })

  it('GIVEN combat HUD rendered WHEN clicking Assist / Pause THEN their own callbacks fire and nothing else does', () => {
    const state = createState('running')
    state.allies = [createDefaultAllyState(1)]
    state.enemies = [
      { id: 1, definitionId: 'enemy-basic', hp: 5, maxHp: 5, x: 360, y: 270, radius: 12, speedPxPerSec: 60, contactDamage: 1, defeated: false, defeatedAtTick: null, faction: 'enemy', role: 'enemy_chaser', behaviorState: 'move_to_engage', targetingPolicy: 'focus_player', targetEntityId: 'player:player-alpha' },
    ]

    hudController.render(state, false, PROD_FIXED_DELTA_MS)

    queryButton(container, 'assist-player').click()
    queryButton(container, 'toggle-pause').click()

    expect(actions.onAssistPlayerCommand).toHaveBeenCalledTimes(1)
    expect(actions.onTogglePause).toHaveBeenCalledTimes(1)
    expect(actions.onClaimReward).not.toHaveBeenCalled()
    expect(actions.onConfirmResult).not.toHaveBeenCalled()
    expect(actions.onNextSortie).not.toHaveBeenCalled()
  })
})

describe('combatHud.ts: buildCombatHudViewModel / getCombatHudAssistStatusCopy (Issue #1375)', () => {
  it('GIVEN elapsedTicks 900 and activeFixedDeltaMs 16 WHEN buildCombatHudViewModel is called THEN elapsedLabel is 14.4 s (AC4, AC8 fixture parity)', () => {
    const state = createState('running')
    state.sortie = { status: 'running', elapsedTicks: 900, targetTicks: 3600, result: null }

    const view = buildCombatHudViewModel(state, false, 16)

    expect(view.elapsedLabel).toBe('14.4 s')
  })

  it('GIVEN non-running phase WHEN getCombatHudAssistStatusCopy is called THEN it reports available during sortie', () => {
    expect(getCombatHudAssistStatusCopy(createState('preparation'))).toBe('Assist is available during sortie.')
  })
})

describe('HudController: data-legacy-result-surface (temporary compatibility, Issue #1375)', () => {
  let container: HTMLElement
  let actions: {
    onAssistPlayerCommand: ReturnType<typeof vi.fn>
    onClaimReward: ReturnType<typeof vi.fn>
    onConfirmResult: ReturnType<typeof vi.fn>
    onNextSortie: ReturnType<typeof vi.fn>
    onTogglePause: ReturnType<typeof vi.fn>
  }
  let hudController: ReturnType<typeof createHudController>

  beforeEach(() => {
    container = document.createElement('div')
    actions = {
      onAssistPlayerCommand: vi.fn(),
      onClaimReward: vi.fn(),
      onConfirmResult: vi.fn(),
      onNextSortie: vi.fn(),
      onTogglePause: vi.fn(),
    }
    hudController = createHudController(container, actions)
  })

  it('GIVEN preparation WHEN render called THEN legacy action surface renders and is not gated by combat-only fields', () => {
    hudController.render(createState('preparation'), false, PROD_FIXED_DELTA_MS)

    expect(container.querySelector('[data-field="loop-phase"]')?.textContent).toBe('Pre-launch')
    expect(queryButton(container, 'claim-reward').disabled).toBe(true)
    expect(queryButton(container, 'confirm-result').disabled).toBe(true)
    expect(queryButton(container, 'next-sortie').disabled).toBe(true)
  })

  it('GIVEN the legacy action surface WHEN rendered THEN interactive buttons opt in via data-battle-interactive', () => {
    hudController.render(createState('preparation'), false, PROD_FIXED_DELTA_MS)

    const interactiveButtons = Array.from(
      legacyResultSurface(container).querySelectorAll<HTMLButtonElement>('[data-action]'),
    )
    expect(interactiveButtons.length).toBeGreaterThan(0)
    expect(interactiveButtons.every((button) => button.dataset.battleInteractive === 'true')).toBe(true)
  })

  it('GIVEN result phase with pending reward WHEN render called THEN confirm-result enabled, claim-reward disabled (AC4, AC5)', () => {
    hudController.render(createState('result', 'pending'), false, PROD_FIXED_DELTA_MS)

    expect(container.querySelector('[data-field="loop-phase"]')?.textContent).toBe('Mission review')
    expect(container.querySelector('[data-field="sortie-status"]')?.textContent).toBe('Area secured')
    expect(container.querySelector('[data-field="sortie-result"]')?.textContent).toBe('Victory')
    // AC5: confirmResult auto-claims; claim-reward is legacy debrief only
    expect(queryButton(container, 'claim-reward').disabled).toBe(true)
    expect(queryButton(container, 'confirm-result').disabled).toBe(false)
    expect(queryButton(container, 'next-sortie').disabled).toBe(true)
  })

  it('GIVEN result phase with claimed reward WHEN render called THEN confirm-result is still enabled (AC5)', () => {
    hudController.render(createState('result', 'claimed'), false, PROD_FIXED_DELTA_MS)

    expect(container.querySelector('[data-field="loop-phase"]')?.textContent).toBe('Mission review')
    expect(queryButton(container, 'confirm-result').disabled).toBe(false)
    expect(queryButton(container, 'claim-reward').disabled).toBe(true)
  })

  it('GIVEN debrief_pending_reward WHEN render called THEN debrief copy enables reward collection only', () => {
    hudController.render(createState('debrief_pending_reward'), false, PROD_FIXED_DELTA_MS)

    expect(container.querySelector('[data-field="loop-phase"]')?.textContent).toBe('Debrief in progress')
    expect(container.querySelector('[data-field="sortie-status"]')?.textContent).toBe('Area secured')
    expect(container.querySelector('[data-field="sortie-result"]')?.textContent).toBe('Victory')
    expect(queryButton(container, 'claim-reward').disabled).toBe(false)
    expect(queryButton(container, 'next-sortie').disabled).toBe(true)
  })

  it('GIVEN debrief_reward_claimed WHEN render called THEN debrief complete copy enables next sortie only', () => {
    hudController.render(createState('debrief_reward_claimed'), false, PROD_FIXED_DELTA_MS)

    expect(container.querySelector('[data-field="loop-phase"]')?.textContent).toBe('Debrief complete')
    expect(container.querySelector('[data-field="sortie-status"]')?.textContent).toBe('Area secured')
    expect(container.querySelector('[data-field="sortie-result"]')?.textContent).toBe('Victory')
    expect(queryButton(container, 'next-sortie').disabled).toBe(false)
    expect(queryButton(container, 'claim-reward').disabled).toBe(true)
  })

  it('GIVEN result feedback copy WHEN render called THEN status region exposes player-facing progress copy without innerHTML', () => {
    const state = createState('debrief_reward_claimed')
    state.telemetry.status = 'Reward claimed for this session.'
    state.telemetry.lastCommandSummary = 'Confirm result to save and return to preparation.'

    hudController.render(state, false, PROD_FIXED_DELTA_MS)

    const status = container.querySelector('[data-field="status"]')
    expect(status?.textContent).toBe('Reward claimed for this session.')
    expect(status?.getAttribute('role')).toBe('status')
    expect(status?.getAttribute('aria-live')).toBe('polite')
    expect(container.querySelector('[data-field="command"]')?.textContent).toBe(
      'Confirm result to save and return to preparation.',
    )
  })

  it('GIVEN preparation WHEN render called THEN the legacy status/command fields keep rendering telemetry unconditionally (phaseScreens.ts prep-status is an additional, not exclusive, surface)', () => {
    const state = createState('preparation')
    state.telemetry.status = 'Save complete.'
    state.telemetry.lastCommandSummary = 'Progression snapshot saved locally.'

    hudController.render(state, false, PROD_FIXED_DELTA_MS)

    expect(container.querySelector('[data-field="status"]')?.textContent).toBe('Save complete.')
    expect(container.querySelector('[data-field="command"]')?.textContent).toBe(
      'Progression snapshot saved locally.',
    )
  })

  it('GIVEN debrief_reward_claimed WHEN disabled claim button is clicked THEN claim callback remains a no-op surface', () => {
    hudController.render(createState('debrief_reward_claimed'), false, PROD_FIXED_DELTA_MS)

    queryButton(container, 'claim-reward').click()
    queryButton(container, 'next-sortie').click()

    expect(actions.onClaimReward).not.toHaveBeenCalled()
    expect(actions.onNextSortie).toHaveBeenCalledTimes(1)
  })

  it('GIVEN HUD rendered WHEN checking text surface THEN normal-play vocabulary boundary is preserved', () => {
    hudController.render(createState('result'), false, PROD_FIXED_DELTA_MS)

    const textSurface = container.textContent ?? ''
    expect(textSurface).not.toContain('title_menu')
    expect(textSurface).not.toContain('load_menu')
    expect(textSurface).not.toContain('debrief_pending_reward')
    expect(textSurface).not.toContain('debrief_reward_claimed')
    expect(textSurface).not.toContain('illegal-transition')
  })
})

// ---------------------------------------------------------------------------
// Issue #914: HUD action harness -- legacy Next sortie / Confirm result success feedback
// ---------------------------------------------------------------------------

describe('Issue #914: HUD action harness -- next-sortie and confirm-result', () => {
  let container: HTMLElement

  beforeEach(() => {
    container = document.createElement('div')
  })

  it('AC1: GIVEN debrief_reward_claimed WHEN next-sortie click via runNextSortieHandler THEN HUD shows "Returned to preparation." / "Use Start sortie to begin the next sortie."', () => {
    const state = createState('debrief_reward_claimed')

    const onNextSortie = vi.fn(() => {
      runNextSortieHandler(state, {
        setHudFeedback: (status, summary) => {
          state.telemetry.status = status
          state.telemetry.lastCommandSummary = summary
        },
      })
      renderHudAfterAction()
    })

    const hudController = createHudController(container, {
      onClaimReward: vi.fn(),
      onConfirmResult: vi.fn(),
      onNextSortie,
      onTogglePause: vi.fn(),
    })

    hudController.render(state, false, PROD_FIXED_DELTA_MS)

    function renderHudAfterAction() {
      hudController.render(state, false, PROD_FIXED_DELTA_MS)
    }

    expect(queryButton(container, 'next-sortie').disabled).toBe(false)
    queryButton(container, 'next-sortie').click()

    expect(onNextSortie).toHaveBeenCalledTimes(1)
    expect(state.loopPhase).toBe('preparation')
    expect(container.querySelector('[data-field="status"]')?.textContent).toBe('Returned to preparation.')
    expect(container.querySelector('[data-field="command"]')?.textContent).toBe(
      'Use Start sortie to begin the next sortie.',
    )
  })

  it('AC2-AC3: GIVEN result + pending reward WHEN confirm-result click via runConfirmResultHandler with fake save success THEN state.telemetry shows "Result confirmed." / "Progress saved locally." and fakeProgressionStorageSave called exactly once', () => {
    const state = createState('result', 'pending')
    const fakeProgressionStorageSave = vi.fn(() => ({ ok: true as const }))

    const onConfirmResult = vi.fn(() => {
      runConfirmResultHandler(state, true, {
        storage: {
          save: fakeProgressionStorageSave,
          load: vi.fn(() => ({ ok: true as const, snapshot: null })),
        },
        createSnapshot: () => createGameSnapshot(state),
        reportSaveFailure: vi.fn(),
        setHudFeedback: (status, summary) => {
          state.telemetry.status = status
          state.telemetry.lastCommandSummary = summary
        },
        resetDebugPause: vi.fn(),
      })
      renderHudAfterAction()
    })

    const hudController = createHudController(container, {
      onClaimReward: vi.fn(),
      onConfirmResult,
      onNextSortie: vi.fn(),
      onTogglePause: vi.fn(),
    })

    hudController.render(state, false, PROD_FIXED_DELTA_MS)

    function renderHudAfterAction() {
      hudController.render(state, false, PROD_FIXED_DELTA_MS)
    }

    expect(queryButton(container, 'confirm-result').disabled).toBe(false)
    queryButton(container, 'confirm-result').click()

    expect(onConfirmResult).toHaveBeenCalledTimes(1)
    expect(state.loopPhase).toBe('preparation')
    expect(container.querySelector('[data-field="status"]')?.textContent).toBe('Result confirmed.')
    expect(container.querySelector('[data-field="command"]')?.textContent).toBe('Progress saved locally.')
    expect(fakeProgressionStorageSave).toHaveBeenCalledTimes(1)
  })
})

// ---------------------------------------------------------------------------
// phaseScreens.ts pure helpers (AC5, AC8)
// ---------------------------------------------------------------------------

describe('phaseScreens: pure helpers', () => {
  it('getVisiblePhaseScreen maps title_menu/load_menu/preparation to their screen id and everything else to null (AC5)', () => {
    expect(getVisiblePhaseScreen('title_menu')).toBe('title')
    expect(getVisiblePhaseScreen('load_menu')).toBe('load')
    expect(getVisiblePhaseScreen('preparation')).toBe('preparation')
    expect(getVisiblePhaseScreen('running')).toBeNull()
    expect(getVisiblePhaseScreen('result')).toBeNull()
    expect(getVisiblePhaseScreen('debrief_pending_reward')).toBeNull()
    expect(getVisiblePhaseScreen('debrief_reward_claimed')).toBeNull()
  })

  it('resolveLoadMenuBackIntent selects back_to_preparation for preparation origin and back_to_title otherwise (AC8)', () => {
    expect(resolveLoadMenuBackIntent('preparation')).toBe('back_to_preparation')
    expect(resolveLoadMenuBackIntent('title_menu')).toBe('back_to_title')
  })
})

// ---------------------------------------------------------------------------
// phaseScreens: createPhaseScreenController (title / load / preparation)
//
// This covers DOM wiring (visibility, disabled state, click delegation,
// upgrade view model rendering) with jsdom. It intentionally does NOT claim
// to validate real accessible-role/name/focus semantics -- that is the
// responsibility of the real Playwright checks in
// tests/e2e/phase-screens.spec.ts (PR #1815 review, required fix 3).
// ---------------------------------------------------------------------------

describe('phaseScreens: createPhaseScreenController', () => {
  let container: HTMLElement
  let actions: {
    onNewGame: ReturnType<typeof vi.fn>
    onOpenLoadMenu: ReturnType<typeof vi.fn>
    onBackFromLoadMenu: ReturnType<typeof vi.fn>
    onConfirmLoad: ReturnType<typeof vi.fn>
    onStartSortie: ReturnType<typeof vi.fn>
    onSave: ReturnType<typeof vi.fn>
    onReset: ReturnType<typeof vi.fn>
    onUpgradeWeapon: ReturnType<typeof vi.fn>
    canLoadGame: ReturnType<typeof vi.fn>
  }
  let controller: ReturnType<typeof createPhaseScreenController>

  beforeEach(() => {
    container = document.createElement('div')
    actions = {
      onNewGame: vi.fn(),
      onOpenLoadMenu: vi.fn(),
      onBackFromLoadMenu: vi.fn(),
      onConfirmLoad: vi.fn(),
      onStartSortie: vi.fn(),
      onSave: vi.fn(),
      onReset: vi.fn(),
      onUpgradeWeapon: vi.fn(),
      canLoadGame: vi.fn(() => true),
    }
    controller = createPhaseScreenController(container, actions)
  })

  it('GIVEN title_menu WHEN render called THEN only the title screen is visible (not hidden/inert) and New Game / Open save exist (AC1)', () => {
    controller.render(createState('title_menu'))

    const title = container.querySelector<HTMLElement>('[data-phase-screen="title"]')
    const load = container.querySelector<HTMLElement>('[data-phase-screen="load"]')
    const preparation = container.querySelector<HTMLElement>('[data-phase-screen="preparation"]')

    expect(title?.hidden).toBe(false)
    expect(title?.hasAttribute('inert')).toBe(false)
    expect(load?.hidden).toBe(true)
    expect(load?.hasAttribute('inert')).toBe(true)
    expect(preparation?.hidden).toBe(true)
    expect(preparation?.hasAttribute('inert')).toBe(true)
    expect(queryButton(container, 'new-game').disabled).toBe(false)
    expect(queryButton(container, 'open-load-menu-title').disabled).toBe(false)
  })

  it('GIVEN preparation WHEN render called THEN the preparation screen is visible and Launch sortie / Save / Reset are enabled (AC2)', () => {
    controller.render(createState('preparation'))

    const preparation = container.querySelector<HTMLElement>('[data-phase-screen="preparation"]')
    expect(preparation?.hidden).toBe(false)
    expect(queryButton(container, 'start-sortie').disabled).toBe(false)
    expect(queryButton(container, 'save').disabled).toBe(false)
    expect(queryButton(container, 'reset').disabled).toBe(false)
    expect(queryButton(container, 'open-load-menu-preparation').disabled).toBe(false)
  })

  it('GIVEN running WHEN render called THEN the outer screen layer and every phase screen are hidden and inert (AC3, AC6)', () => {
    controller.render(createState('running'))

    expect(container.hidden).toBe(true)
    expect(container.hasAttribute('inert')).toBe(true)
    expect(queryButton(container, 'new-game').disabled).toBe(true)
    expect(queryButton(container, 'start-sortie').disabled).toBe(true)
    expect(queryButton(container, 'save').disabled).toBe(true)
    expect(queryButton(container, 'reset').disabled).toBe(true)
    expect(queryButton(container, 'open-load-menu-title').disabled).toBe(true)
    expect(queryButton(container, 'open-load-menu-preparation').disabled).toBe(true)
  })

  it('GIVEN title_menu WHEN Open save is clicked THEN onOpenLoadMenu fires with origin title_menu (intent-only, AC8)', () => {
    controller.render(createState('title_menu'))

    queryButton(container, 'open-load-menu-title').click()

    expect(actions.onOpenLoadMenu).toHaveBeenCalledWith('title_menu')
  })

  it('GIVEN preparation WHEN Open save is clicked THEN onOpenLoadMenu fires with origin preparation (intent-only, AC8)', () => {
    controller.render(createState('preparation'))

    queryButton(container, 'open-load-menu-preparation').click()

    expect(actions.onOpenLoadMenu).toHaveBeenCalledWith('preparation')
  })

  it('GIVEN load_menu WHEN Back is clicked THEN onBackFromLoadMenu fires (intent-only -- origin bookkeeping lives in main.ts, AC8)', () => {
    controller.render(createState('load_menu'))

    queryButton(container, 'back-from-load-menu').click()

    expect(actions.onBackFromLoadMenu).toHaveBeenCalledTimes(1)
  })

  it('GIVEN load_menu WHEN Load saved game is clicked THEN onConfirmLoad fires (AC9)', () => {
    controller.render(createState('load_menu'))

    queryButton(container, 'confirm-load').click()

    expect(actions.onConfirmLoad).toHaveBeenCalledTimes(1)
  })

  it('GIVEN load_menu with a load failure telemetry message WHEN rendered THEN the failure message shows inside the load screen itself (loadFailure, AC9)', () => {
    const state = createState('load_menu')
    state.telemetry.status = 'Load Game failed.'
    state.telemetry.lastCommandSummary = 'No save data found.'

    controller.render(state)

    expect(container.querySelector('[data-field="load-status"]')?.textContent).toBe('Load Game failed.')
    const loadScreen = container.querySelector<HTMLElement>('[data-phase-screen="load"]')
    expect(loadScreen?.hidden).toBe(false)
    expect(state.loopPhase).toBe('load_menu')
  })

  it('GIVEN no loadable snapshot WHEN render called THEN navigating to load_menu is still reachable, but Load saved game stays disabled (AC1, AC3, AC9)', () => {
    // The load-menu-empty scenario (PR #1815 review, required fix 6) depends
    // on being able to open load_menu with no save present and see "no
    // save" messaging there -- only the confirm action is gated.
    actions.canLoadGame.mockReturnValue(false)

    controller.render(createState('title_menu'))
    expect(queryButton(container, 'open-load-menu-title').disabled).toBe(false)

    controller.render(createState('load_menu'))
    expect(queryButton(container, 'confirm-load').disabled).toBe(true)
  })

  it('GIVEN preparation WHEN render called with an upgradeView THEN Weapon Power, cost, and status render (AC2)', () => {
    controller.render(createState('preparation'), {
      definitionId: 'weapon_power_plus_1',
      cost: 100,
      weaponPower: 9,
      buttonDisabled: false,
      statusCopy: getUpgradeStatusCopy('ok'),
    })

    expect(container.querySelector('[data-field="prep-weapon-power"]')?.textContent).toBe('9')
    expect(container.querySelector('[data-field="prep-upgrade-cost"]')?.textContent).toBe('Cost: 100')
    expect(container.querySelector('[data-field="prep-upgrade-status"]')?.textContent).toBe(
      'Upgrade installed. Weapon Power increased. Resources were saved.',
    )
    expect(queryButton(container, 'upgrade-weapon').disabled).toBe(false)
  })

  it('GIVEN preparation WHEN render called without an upgradeView THEN the upgrade button is disabled and falls back to state.progress.weaponPower (fail-closed default)', () => {
    const state = createState('preparation')
    state.progress.weaponPower = 4

    controller.render(state)

    expect(container.querySelector('[data-field="prep-weapon-power"]')?.textContent).toBe('4')
    expect(queryButton(container, 'upgrade-weapon').disabled).toBe(true)
    expect(container.querySelector('[data-field="prep-upgrade-cost"]')?.textContent).toBe('')
  })

  it('GIVEN an enabled upgrade button WHEN it is clicked THEN onUpgradeWeapon fires exactly once', () => {
    controller.render(createState('preparation'), {
      definitionId: 'weapon_power_plus_1',
      cost: 100,
      weaponPower: 1,
      buttonDisabled: false,
      statusCopy: null,
    })

    queryButton(container, 'upgrade-weapon').click()

    expect(actions.onUpgradeWeapon).toHaveBeenCalledTimes(1)
  })

  it('does not leak internal upgrade failure reason (AC4 mapping table boundary)', () => {
    controller.render(createState('preparation'), {
      definitionId: 'weapon_power_plus_1',
      cost: 100,
      weaponPower: 1,
      buttonDisabled: true,
      statusCopy: getUpgradeStatusCopy('insufficient-resources'),
    })

    const textSurface = container.textContent ?? ''
    expect(textSurface).not.toContain('insufficient-resources')
    expect(textSurface).not.toContain('already-purchased')
    expect(textSurface).not.toContain('not-preparation')
    expect(textSurface).toContain('Not enough resources.')
    expect(textSurface).toContain('Earn 100 resources before upgrading.')
  })

  it('GIVEN each purchase outcome reason WHEN getUpgradeStatusCopy builds player-facing copy THEN it matches the AC4 mapping table', () => {
    expect(getUpgradeStatusCopy('ok')).toEqual({
      status: 'Upgrade installed.',
      summary: 'Weapon Power increased. Resources were saved.',
    })
    expect(getUpgradeStatusCopy('write-error')).toEqual({
      status: 'Upgrade not saved.',
      summary: 'No resources were spent. Check browser storage and try again.',
    })
    expect(getUpgradeStatusCopy('invalid-state')).toEqual({
      status: 'Upgrade unavailable.',
      summary: 'Current upgrade data could not be applied.',
    })
  })

  it('GIVEN any state WHEN rendered THEN no raw LoopPhase enum copy leaks into overlay text', () => {
    controller.render(createState('load_menu'))

    const textSurface = container.textContent ?? ''
    expect(textSurface).not.toContain('title_menu')
    expect(textSurface).not.toContain('load_menu')
    expect(textSurface).not.toContain('debrief_pending_reward')
    expect(textSurface).not.toContain('debrief_reward_claimed')
  })

  // -------------------------------------------------------------------
  // Issue #1375 In Scope: preparation Save/Reset/New Game feedback now
  // renders inside this screen's own prep-status field.
  // -------------------------------------------------------------------

  it('GIVEN preparation with a Save success telemetry message WHEN rendered THEN prep-status shows the feedback (In Scope, Issue #1375)', () => {
    const state = createState('preparation')
    state.telemetry.status = 'Save complete.'
    state.telemetry.lastCommandSummary = 'Progression snapshot saved locally.'

    controller.render(state)

    expect(container.querySelector('[data-field="prep-status"]')?.textContent).toBe(
      'Save complete. Progression snapshot saved locally.',
    )
    expect(container.querySelector('[data-field="prep-status"]')?.getAttribute('role')).toBe('status')
    expect(container.querySelector('[data-field="prep-status"]')?.getAttribute('aria-live')).toBe('polite')
  })

  it('GIVEN a non-preparation phase WHEN rendered THEN prep-status renders nothing (feedback stays phase-scoped)', () => {
    const state = createState('title_menu')
    state.telemetry.status = 'Load Game failed.'
    state.telemetry.lastCommandSummary = 'No save data available.'

    controller.render(state)

    expect(container.querySelector('[data-field="prep-status"]')?.textContent).toBe('')
  })
})
