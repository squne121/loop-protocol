/**
 * @vitest-environment jsdom
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createHudController, getUpgradeStatusCopy } from '../src/ui/HudController'
import {
  runNextSortieHandler,
  runConfirmResultHandler,
} from '../src/main'
import type { GameState, LoopPhase, ResultRewardStatus, SortieResult } from '../src/state'
import { createDefaultAllyState, createGameSnapshot } from '../src/state'

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

describe('HudController', () => {
  let container: HTMLElement
  let actions: {
    onNewGame: ReturnType<typeof vi.fn>
    onStartSortie: ReturnType<typeof vi.fn>
    onAssistPlayerCommand: ReturnType<typeof vi.fn>
    onClaimReward: ReturnType<typeof vi.fn>
    onConfirmResult: ReturnType<typeof vi.fn>
    onNextSortie: ReturnType<typeof vi.fn>
    onSave: ReturnType<typeof vi.fn>
    onLoadGame: ReturnType<typeof vi.fn>
    onReset: ReturnType<typeof vi.fn>
    canLoadGame: ReturnType<typeof vi.fn>
    onTogglePause: ReturnType<typeof vi.fn>
  }
  let hudController: ReturnType<typeof createHudController>

  beforeEach(() => {
    container = document.createElement('div')
    actions = {
      onNewGame: vi.fn(),
      onStartSortie: vi.fn(),
      onAssistPlayerCommand: vi.fn(),
      onClaimReward: vi.fn(),
      onConfirmResult: vi.fn(),
      onNextSortie: vi.fn(),
      onSave: vi.fn(),
      onLoadGame: vi.fn(),
      onReset: vi.fn(),
      canLoadGame: vi.fn(() => true),
      onTogglePause: vi.fn(),
    }
    hudController = createHudController(container, actions)
  })

  it('GIVEN preparation WHEN render called THEN phase copy and preparation actions are enabled', () => {
    hudController.render(createState('preparation'), false)

    expect(container.querySelector('[data-field="loop-phase"]')?.textContent).toBe('Pre-launch')
    expect(queryButton(container, 'start-sortie').disabled).toBe(false)
    expect(queryButton(container, 'save').disabled).toBe(false)
    expect(queryButton(container, 'reset').disabled).toBe(false)
    expect(queryButton(container, 'claim-reward').disabled).toBe(true)
    expect(queryButton(container, 'confirm-result').disabled).toBe(true)
    expect(queryButton(container, 'next-sortie').disabled).toBe(true)
    expect(queryButton(container, 'load-game').disabled).toBe(true)
    expect(queryButton(container, 'reset').getAttribute('title')).toContain('destructive boundary')
  })

  it('GIVEN title_menu WHEN render called THEN new-game enabled, load-game enabled, start-sortie disabled (AC1)', () => {
    hudController.render(createState('title_menu'), false)

    expect(container.querySelector('[data-field="loop-phase"]')?.textContent).toBe('Launch setup')
    expect(queryButton(container, 'new-game').disabled).toBe(false)
    expect(queryButton(container, 'load-game').disabled).toBe(false)
    expect(queryButton(container, 'start-sortie').disabled).toBe(true)
    expect(queryButton(container, 'save').disabled).toBe(true)
    expect(queryButton(container, 'confirm-result').disabled).toBe(true)
    expect(queryButton(container, 'next-sortie').disabled).toBe(true)
  })

  it('GIVEN title_menu without loadable snapshot WHEN render called THEN new-game enabled, load-game disabled (AC1)', () => {
    actions.canLoadGame.mockReturnValue(false)
    hudController.render(createState('title_menu'), false)

    expect(queryButton(container, 'new-game').disabled).toBe(false)
    expect(queryButton(container, 'load-game').disabled).toBe(true)
    expect(queryButton(container, 'start-sortie').disabled).toBe(true)
  })

  it('GIVEN load_menu WHEN render called THEN load-game is enabled and save is disabled', () => {
    hudController.render(createState('load_menu'), false)

    expect(container.querySelector('[data-field="loop-phase"]')?.textContent).toBe('Restore briefing')
    expect(queryButton(container, 'load-game').disabled).toBe(false)
    expect(queryButton(container, 'save').disabled).toBe(true)
    expect(queryButton(container, 'start-sortie').disabled).toBe(true)
    expect(queryButton(container, 'confirm-result').disabled).toBe(true)
  })

  it('GIVEN title_menu without a loadable snapshot WHEN render called THEN load-game is disabled (AC9)', () => {
    actions.canLoadGame.mockReturnValue(false)

    hudController.render(createState('title_menu'), false)

    expect(queryButton(container, 'load-game').disabled).toBe(true)
  })

  it('GIVEN running WHEN render called THEN button.disabled marks the full action surface as disabled', () => {
    hudController.render(createState('running'), false)

    expect(container.querySelector('[data-field="loop-phase"]')?.textContent).toBe('Sortie active')
    expect(queryButton(container, 'start-sortie').disabled).toBe(true)
    expect(queryButton(container, 'claim-reward').disabled).toBe(true)
    expect(queryButton(container, 'confirm-result').disabled).toBe(true)
    expect(queryButton(container, 'next-sortie').disabled).toBe(true)
    expect(queryButton(container, 'save').disabled).toBe(true)
    expect(queryButton(container, 'load-game').disabled).toBe(true)
    expect(queryButton(container, 'reset').disabled).toBe(true)
    expect(queryButton(container, 'assist-player').disabled).toBe(false)
  })

  it('GIVEN the HUD action surface WHEN rendered THEN interactive buttons opt in via data-battle-interactive', () => {
    // Overlay inactivity is enforced by the shell layer via hidden/inert;
    // this test covers the complementary HUD-side pointer opt-in contract.
    hudController.render(createState('preparation'), false, {
      definitionId: 'weapon_power_plus_1',
      cost: 100,
      weaponPower: 1,
      buttonDisabled: false,
      statusCopy: null,
    })

    const interactiveButtons = Array.from(
      container.querySelectorAll<HTMLButtonElement>('[data-action]'),
    )
    expect(interactiveButtons.length).toBeGreaterThan(0)
    expect(interactiveButtons.every((button) => button.dataset.battleInteractive === 'true')).toBe(true)
  })

  it('GIVEN running WHEN render called THEN disabled overlay buttons remain present but are inert-ready hit-test surfaces', () => {
    hudController.render(createState('running'), false)

    expect(queryButton(container, 'start-sortie').disabled).toBe(true)
    expect(queryButton(container, 'start-sortie').dataset.battleInteractive).toBe('true')
    expect(queryButton(container, 'claim-reward').disabled).toBe(true)
    expect(queryButton(container, 'claim-reward').dataset.battleInteractive).toBe('true')
  })

  it('GIVEN running with ally and living enemy WHEN render called THEN assist status reports ready and assist button is reachable', () => {
    const state = createState('running')
    state.allies = [createDefaultAllyState(1)]
    state.enemies = [
      {
        id: 1,
        definitionId: 'enemy-basic',
        hp: 5,
        maxHp: 5,
        x: 360,
        y: 270,
        radius: 12,
        speedPxPerSec: 60,
        contactDamage: 1,
        defeated: false,
        defeatedAtTick: null,
        faction: 'enemy',
        role: 'enemy_chaser',
        behaviorState: 'move_to_engage',
        targetingPolicy: 'focus_player',
        targetEntityId: 'player:player-alpha',
      },
    ]

    hudController.render(state, false)

    const assistStatus = container.querySelector('[data-field="assist-status"]')
    expect(queryButton(container, 'assist-player').disabled).toBe(false)
    expect(assistStatus?.textContent).toBe('Assist ready.')
    expect(assistStatus?.getAttribute('role')).toBe('status')
    expect(assistStatus?.getAttribute('aria-live')).toBe('polite')
    expect(assistStatus?.getAttribute('aria-atomic')).toBe('true')
  })

  it('GIVEN running without allies WHEN render called THEN assist status reports no ally available', () => {
    const state = createState('running')
    state.enemies = [
      {
        id: 1,
        definitionId: 'enemy-basic',
        hp: 5,
        maxHp: 5,
        x: 360,
        y: 270,
        radius: 12,
        speedPxPerSec: 60,
        contactDamage: 1,
        defeated: false,
        defeatedAtTick: null,
        faction: 'enemy',
        role: 'enemy_chaser',
        behaviorState: 'move_to_engage',
        targetingPolicy: 'focus_player',
        targetEntityId: 'player:player-alpha',
      },
    ]

    hudController.render(state, false)

    expect(container.querySelector('[data-field="assist-status"]')?.textContent).toBe(
      'No ally available.',
    )
  })

  it('GIVEN running with ally but no valid target WHEN render called THEN assist status reports no target to assist', () => {
    const state = createState('running')
    state.allies = [createDefaultAllyState(1)]

    hudController.render(state, false)

    expect(container.querySelector('[data-field="assist-status"]')?.textContent).toBe(
      'No target to assist.',
    )
  })

  it('GIVEN running WHEN render called THEN HUD keeps overlay-stack layout', () => {
    hudController.render(createState('running'), false)

    expect(container.dataset.battleHudLayout).toBe('overlay-stack')
  })

  it('GIVEN result WHEN render called THEN HUD exposes result-header layout for canvas-safe actions', () => {
    hudController.render(createState('result'), false)

    expect(container.dataset.battleHudLayout).toBe('result-header')
    expect(container.querySelector('.panel--actions')?.hasAttribute('hidden')).toBe(false)
    expect(container.querySelector('.panel--pause-status')?.hasAttribute('hidden')).toBe(false)
    expect(container.querySelector('.panel--accent')?.hidden).toBe(true)
  })

  it('GIVEN active assist without assigned target WHEN render called THEN assist status reports signal sent', () => {
    const state = createState('running')
    state.allies = [createDefaultAllyState(1)]
    state.enemies = [
      {
        id: 1,
        definitionId: 'enemy-basic',
        hp: 5,
        maxHp: 5,
        x: 360,
        y: 270,
        radius: 12,
        speedPxPerSec: 60,
        contactDamage: 1,
        defeated: false,
        defeatedAtTick: null,
        faction: 'enemy',
        role: 'enemy_chaser',
        behaviorState: 'move_to_engage',
        targetingPolicy: 'focus_player',
        targetEntityId: 'player:player-alpha',
      },
    ]
    state.commandIntentRuntime.activeIntent = 'assist_player'

    hudController.render(state, false)

    expect(container.querySelector('[data-field="assist-status"]')?.textContent).toBe(
      'Assist signal sent.',
    )
  })

  it('GIVEN active assist with assigned target WHEN render called THEN assist status reports allies covering you', () => {
    const state = createState('running')
    const ally = createDefaultAllyState(1)
    ally.targetEntityId = 'enemy:1'
    state.allies = [ally]
    state.enemies = [
      {
        id: 1,
        definitionId: 'enemy-basic',
        hp: 5,
        maxHp: 5,
        x: 360,
        y: 270,
        radius: 12,
        speedPxPerSec: 60,
        contactDamage: 1,
        defeated: false,
        defeatedAtTick: null,
        faction: 'enemy',
        role: 'enemy_chaser',
        behaviorState: 'move_to_engage',
        targetingPolicy: 'focus_player',
        targetEntityId: 'player:player-alpha',
      },
    ]
    state.commandIntentRuntime.activeIntent = 'assist_player'

    hudController.render(state, false)

    expect(container.querySelector('[data-field="assist-status"]')?.textContent).toBe(
      'Allies covering you.',
    )
  })

  it('GIVEN non-running phase WHEN render called THEN assist status reports available during sortie', () => {
    hudController.render(createState('preparation'), false)

    expect(container.querySelector('[data-field="assist-status"]')?.textContent).toBe(
      'Assist is available during sortie.',
    )
    expect(queryButton(container, 'assist-player').disabled).toBe(true)
  })

  it('GIVEN result phase with pending reward WHEN render called THEN confirm-result enabled, claim-reward disabled (AC4, AC5)', () => {
    hudController.render(createState('result', 'pending'), false)

    expect(container.querySelector('[data-field="loop-phase"]')?.textContent).toBe('Mission review')
    expect(container.querySelector('[data-field="sortie-status"]')?.textContent).toBe('Area secured')
    expect(container.querySelector('[data-field="sortie-result"]')?.textContent).toBe('Victory')
    // AC5: confirmResult auto-claims; claim-reward is legacy debrief only
    expect(queryButton(container, 'claim-reward').disabled).toBe(true)
    expect(queryButton(container, 'confirm-result').disabled).toBe(false)
    expect(queryButton(container, 'start-sortie').disabled).toBe(true)
    expect(queryButton(container, 'next-sortie').disabled).toBe(true)
    expect(queryButton(container, 'save').disabled).toBe(true)
    expect(queryButton(container, 'load-game').disabled).toBe(true)
    expect(queryButton(container, 'reset').disabled).toBe(true)
  })

  it('GIVEN result phase with claimed reward WHEN render called THEN confirm-result is still enabled (AC5)', () => {
    hudController.render(createState('result', 'claimed'), false)

    expect(container.querySelector('[data-field="loop-phase"]')?.textContent).toBe('Mission review')
    expect(queryButton(container, 'confirm-result').disabled).toBe(false)
    expect(queryButton(container, 'claim-reward').disabled).toBe(true)
    expect(queryButton(container, 'start-sortie').disabled).toBe(true)
    expect(queryButton(container, 'save').disabled).toBe(true)
  })

  it('GIVEN debrief_pending_reward WHEN render called THEN debrief copy enables reward collection only', () => {
    hudController.render(createState('debrief_pending_reward'), false)

    expect(container.querySelector('[data-field="loop-phase"]')?.textContent).toBe('Debrief in progress')
    expect(container.querySelector('[data-field="sortie-status"]')?.textContent).toBe('Area secured')
    expect(container.querySelector('[data-field="sortie-result"]')?.textContent).toBe('Victory')
    expect(queryButton(container, 'claim-reward').disabled).toBe(false)
    expect(queryButton(container, 'start-sortie').disabled).toBe(true)
    expect(queryButton(container, 'next-sortie').disabled).toBe(true)
    expect(queryButton(container, 'save').disabled).toBe(true)
    expect(queryButton(container, 'load-game').disabled).toBe(true)
    expect(queryButton(container, 'reset').disabled).toBe(true)
  })

  it('GIVEN debrief_reward_claimed WHEN render called THEN debrief complete copy enables next sortie only', () => {
    hudController.render(createState('debrief_reward_claimed'), false)

    expect(container.querySelector('[data-field="loop-phase"]')?.textContent).toBe('Debrief complete')
    expect(container.querySelector('[data-field="sortie-status"]')?.textContent).toBe('Area secured')
    expect(container.querySelector('[data-field="sortie-result"]')?.textContent).toBe('Victory')
    expect(queryButton(container, 'next-sortie').disabled).toBe(false)
    expect(queryButton(container, 'start-sortie').disabled).toBe(true)
    expect(queryButton(container, 'claim-reward').disabled).toBe(true)
    expect(queryButton(container, 'save').disabled).toBe(true)
    expect(queryButton(container, 'load-game').disabled).toBe(true)
    expect(queryButton(container, 'reset').disabled).toBe(true)
  })

  it('GIVEN feedback copy WHEN render called THEN status region exposes player-facing progress copy without innerHTML', () => {
    const state = createState('debrief_reward_claimed')
    state.telemetry.status = 'Reward claimed for this session.'
    state.telemetry.lastCommandSummary = 'Confirm result to save and return to preparation.'

    hudController.render(state, false)

    const status = container.querySelector('[data-field="status"]')
    expect(status?.textContent).toBe('Reward claimed for this session.')
    expect(status?.getAttribute('role')).toBe('status')
    expect(status?.getAttribute('aria-live')).toBe('polite')
    expect(container.querySelector('[data-field="command"]')?.textContent).toBe(
      'Confirm result to save and return to preparation.',
    )
  })

  it('GIVEN running WHEN disabled buttons are clicked THEN callbacks are not invoked', () => {
    hudController.render(createState('running'), false)

    queryButton(container, 'new-game').click()
    queryButton(container, 'start-sortie').click()
    queryButton(container, 'claim-reward').click()
    queryButton(container, 'confirm-result').click()
    queryButton(container, 'next-sortie').click()
    queryButton(container, 'save').click()
    queryButton(container, 'load-game').click()
    queryButton(container, 'reset').click()
    queryButton(container, 'assist-player').click()

    expect(actions.onNewGame).not.toHaveBeenCalled()
    expect(actions.onStartSortie).not.toHaveBeenCalled()
    expect(actions.onClaimReward).not.toHaveBeenCalled()
    expect(actions.onConfirmResult).not.toHaveBeenCalled()
    expect(actions.onNextSortie).not.toHaveBeenCalled()
    expect(actions.onSave).not.toHaveBeenCalled()
    expect(actions.onLoadGame).not.toHaveBeenCalled()
    expect(actions.onReset).toHaveBeenCalledTimes(0)
    expect(actions.onAssistPlayerCommand).toHaveBeenCalledTimes(1)
  })

  it('GIVEN HUD rendered WHEN checking text surface THEN normal-play vocabulary boundary is preserved', () => {
    hudController.render(createState('preparation'), false)

    const textSurface = container.textContent ?? ''
    expect(textSurface).toContain('Progress')
    expect(textSurface).toContain('Pilot updates')
    expect(textSurface).not.toContain('Loop Phase')
    expect(textSurface).not.toContain('Telemetry')
    expect(textSurface).not.toContain('Claim reward')
    expect(textSurface).not.toContain('title_menu')
    expect(textSurface).not.toContain('load_menu')
    expect(textSurface).not.toContain('debrief_pending_reward')
    expect(textSurface).not.toContain('debrief_reward_claimed')
    expect(textSurface).not.toContain('illegal-transition')
  })

  it('GIVEN HUD rendered WHEN reading mission copy THEN player-facing copy boundary is preserved', () => {
    hudController.render(createState('debrief_pending_reward'), false)

    expect(container.querySelector('[data-field="loop-phase"]')?.textContent).toBe('Debrief in progress')
    expect(container.querySelector('[data-field="sortie-status"]')?.textContent).toBe('Area secured')
    expect(container.querySelector('[data-field="sortie-result"]')?.textContent).toBe('Victory')
  })

  it('GIVEN debrief_reward_claimed WHEN disabled claim button is clicked THEN claim callback remains a no-op surface', () => {
    hudController.render(createState('debrief_reward_claimed'), false)

    queryButton(container, 'claim-reward').click()
    queryButton(container, 'next-sortie').click()

    expect(actions.onClaimReward).not.toHaveBeenCalled()
    expect(actions.onNextSortie).toHaveBeenCalledTimes(1)
  })
})

// ---------------------------------------------------------------------------
// AC3: Load Game spy test — storage.load() only called from title_menu / load_menu
// ---------------------------------------------------------------------------

describe('AC3: Load Game phase gate — onLoadGame only fires from title_menu / load_menu', () => {
  let container: HTMLElement
  let onLoadGame: ReturnType<typeof vi.fn>
  let canLoadGame: ReturnType<typeof vi.fn>
  let hudController: ReturnType<typeof createHudController>

  beforeEach(() => {
    container = document.createElement('div')
    onLoadGame = vi.fn()
    canLoadGame = vi.fn(() => true)
    hudController = createHudController(container, {
      onNewGame: vi.fn(),
      onStartSortie: vi.fn(),
      onClaimReward: vi.fn(),
      onConfirmResult: vi.fn(),
      onNextSortie: vi.fn(),
      onSave: vi.fn(),
      onLoadGame,
      onReset: vi.fn(),
      canLoadGame,
      onTogglePause: vi.fn(),
    })
  })

  it('GIVEN title_menu with loadable snapshot WHEN load-game button is clicked THEN onLoadGame is called once (AC3)', () => {
    hudController.render(createState('title_menu'), false)

    expect(queryButton(container, 'load-game').disabled).toBe(false)
    queryButton(container, 'load-game').click()

    expect(onLoadGame).toHaveBeenCalledTimes(1)
  })

  it('GIVEN load_menu with loadable snapshot WHEN load-game button is clicked THEN onLoadGame is called once (AC3)', () => {
    hudController.render(createState('load_menu'), false)

    expect(queryButton(container, 'load-game').disabled).toBe(false)
    queryButton(container, 'load-game').click()

    expect(onLoadGame).toHaveBeenCalledTimes(1)
  })

  it('GIVEN preparation phase WHEN load-game button is clicked THEN onLoadGame is NOT called (AC3)', () => {
    hudController.render(createState('preparation'), false)

    expect(queryButton(container, 'load-game').disabled).toBe(true)
    queryButton(container, 'load-game').click()

    expect(onLoadGame).not.toHaveBeenCalled()
  })

  it('GIVEN running phase WHEN load-game button is clicked THEN onLoadGame is NOT called (AC3)', () => {
    hudController.render(createState('running'), false)

    expect(queryButton(container, 'load-game').disabled).toBe(true)
    queryButton(container, 'load-game').click()

    expect(onLoadGame).not.toHaveBeenCalled()
  })

  it('GIVEN result phase WHEN load-game button is clicked THEN onLoadGame is NOT called (AC3)', () => {
    hudController.render(createState('result', 'pending'), false)

    expect(queryButton(container, 'load-game').disabled).toBe(true)
    queryButton(container, 'load-game').click()

    expect(onLoadGame).not.toHaveBeenCalled()
  })
})

// ---------------------------------------------------------------------------
// Issue #914: HUD action harness — legacy Next sortie / Confirm result success feedback
// ---------------------------------------------------------------------------

describe('Issue #914: HUD action harness — next-sortie and confirm-result', () => {
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
      onNewGame: vi.fn(),
      onStartSortie: vi.fn(),
      onClaimReward: vi.fn(),
      onConfirmResult: vi.fn(),
      onNextSortie,
      onSave: vi.fn(),
      onLoadGame: vi.fn(),
      onReset: vi.fn(),
      canLoadGame: vi.fn(() => true),
      onTogglePause: vi.fn(),
    })

    hudController.render(state, false)

    function renderHudAfterAction() {
      hudController.render(state, false)
    }

    expect(queryButton(container, 'next-sortie').disabled).toBe(false)
    queryButton(container, 'next-sortie').click()

    expect(onNextSortie).toHaveBeenCalledTimes(1)
    expect(state.loopPhase).toBe('preparation')
    expect(container.querySelector('[data-field="status"]')?.textContent).toBe('Returned to preparation.')
    expect(container.querySelector('[data-field="command"]')?.textContent).toBe('Use Start sortie to begin the next sortie.')
  })

  it('AC2-AC3: GIVEN result + pending reward WHEN confirm-result click via runConfirmResultHandler with fake save success THEN HUD shows "Result confirmed." / "Progress saved locally." and fakeProgressionStorageSave called exactly once', () => {
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
      onNewGame: vi.fn(),
      onStartSortie: vi.fn(),
      onClaimReward: vi.fn(),
      onConfirmResult,
      onNextSortie: vi.fn(),
      onSave: vi.fn(),
      onLoadGame: vi.fn(),
      onReset: vi.fn(),
      canLoadGame: vi.fn(() => true),
      onTogglePause: vi.fn(),
    })

    hudController.render(state, false)

    function renderHudAfterAction() {
      hudController.render(state, false)
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
// Issue #1282: HUD upgrade purchase surface (AC1, AC2, AC3, AC4, AC5)
// ---------------------------------------------------------------------------

describe('Issue #1282: HUD upgrade purchase surface (AC1, AC2, AC3, AC5)', () => {
  let container: HTMLElement
  let onUpgradeWeapon: ReturnType<typeof vi.fn>
  let hudController: ReturnType<typeof createHudController>

  beforeEach(() => {
    container = document.createElement('div')
    onUpgradeWeapon = vi.fn()
    hudController = createHudController(container, {
      onStartSortie: vi.fn(),
      onClaimReward: vi.fn(),
      onNextSortie: vi.fn(),
      onReset: vi.fn(),
      onTogglePause: vi.fn(),
      onUpgradeWeapon,
    })
  })

  it('GIVEN state.progress.weaponPower WHEN render called THEN the Weapon Power stat displays it (AC1)', () => {
    const state = createState('preparation')
    state.progress.weaponPower = 3

    hudController.render(state, false)

    expect(container.querySelector('[data-field="weapon-power"]')?.textContent).toBe('3')
  })

  it('GIVEN an upgradeView with a weaponPower distinct from state.progress.weaponPower WHEN render called THEN the Weapon Power stat displays upgradeView.weaponPower (view-model-driven, not a direct state read)', () => {
    // Regression test for PR #1365 iteration-2 P2 fix_delta: HudController.render()
    // must read weaponPower from the upgrade view model (when provided) instead
    // of reaching into state.progress directly, so the view-model boundary
    // documented for the rest of HudUpgradeViewModel is not silently broken
    // for this one field.
    const state = createState('preparation')
    state.progress.weaponPower = 1

    hudController.render(state, false, {
      definitionId: 'weapon_power_plus_1',
      cost: 100,
      weaponPower: 9,
      buttonDisabled: false,
      statusCopy: null,
    })

    expect(container.querySelector('[data-field="weapon-power"]')?.textContent).toBe('9')
  })

  it('GIVEN no upgradeView WHEN render called THEN the Weapon Power stat falls back to state.progress.weaponPower', () => {
    const state = createState('preparation')
    state.progress.weaponPower = 4

    hudController.render(state, false)

    expect(container.querySelector('[data-field="weapon-power"]')?.textContent).toBe('4')
  })

  it('GIVEN an upgradeView WHEN render called THEN the Upgrade weapon button, cost, and a role=status/aria-live=polite/aria-atomic=true live region are present (AC2)', () => {
    hudController.render(createState('preparation'), false, {
      definitionId: 'weapon_power_plus_1',
      cost: 100,
      weaponPower: 1,
      buttonDisabled: false,
      statusCopy: null,
    })

    const upgradeButton = queryButton(container, 'upgrade-weapon')
    expect(upgradeButton.textContent).toBe('Upgrade weapon')
    expect(container.querySelector('[data-field="upgrade-cost"]')?.textContent).toBe('Cost: 100')

    const upgradeStatus = container.querySelector('[data-field="upgrade-status"]')
    expect(upgradeStatus?.getAttribute('role')).toBe('status')
    expect(upgradeStatus?.getAttribute('aria-live')).toBe('polite')
    expect(upgradeStatus?.getAttribute('aria-atomic')).toBe('true')
  })

  it('GIVEN upgradeView.buttonDisabled=false during a non-preparation phase WHEN render called THEN the button reflects quoteUpgrade()-derived state, not a HUD-local phase check (AC3)', () => {
    hudController.render(createState('running'), false, {
      definitionId: 'weapon_power_plus_1',
      cost: 100,
      weaponPower: 1,
      buttonDisabled: false,
      statusCopy: null,
    })

    expect(queryButton(container, 'upgrade-weapon').disabled).toBe(false)
  })

  it('GIVEN upgradeView.buttonDisabled=true during preparation phase WHEN render called THEN the button is disabled (AC3)', () => {
    hudController.render(createState('preparation'), false, {
      definitionId: 'weapon_power_plus_1',
      cost: 100,
      weaponPower: 1,
      buttonDisabled: true,
      statusCopy: null,
    })

    expect(queryButton(container, 'upgrade-weapon').disabled).toBe(true)
  })

  it('GIVEN no upgradeView WHEN render called THEN the upgrade button is disabled and cost/status fields are empty (fail-closed default)', () => {
    hudController.render(createState('preparation'), false)

    expect(queryButton(container, 'upgrade-weapon').disabled).toBe(true)
    expect(container.querySelector('[data-field="upgrade-cost"]')?.textContent).toBe('')
    expect(container.querySelector('[data-field="upgrade-status"]')?.textContent).toBe('')
  })

  it('GIVEN an enabled upgrade button WHEN it is clicked THEN onUpgradeWeapon fires exactly once', () => {
    hudController.render(createState('preparation'), false, {
      definitionId: 'weapon_power_plus_1',
      cost: 100,
      weaponPower: 1,
      buttonDisabled: false,
      statusCopy: null,
    })

    queryButton(container, 'upgrade-weapon').click()

    expect(onUpgradeWeapon).toHaveBeenCalledTimes(1)
  })

  it('GIVEN a disabled upgrade button WHEN it is clicked THEN onUpgradeWeapon is not invoked', () => {
    hudController.render(createState('preparation'), false, {
      definitionId: 'weapon_power_plus_1',
      cost: 100,
      weaponPower: 1,
      buttonDisabled: true,
      statusCopy: null,
    })

    queryButton(container, 'upgrade-weapon').click()

    expect(onUpgradeWeapon).not.toHaveBeenCalled()
  })

  it('GIVEN each purchase outcome reason WHEN getUpgradeStatusCopy builds player-facing copy THEN it matches the AC4 mapping table', () => {
    expect(getUpgradeStatusCopy('ok')).toEqual({
      status: 'Upgrade installed.',
      summary: 'Weapon Power increased. Resources were saved.',
    })
    expect(getUpgradeStatusCopy('insufficient-resources')).toEqual({
      status: 'Not enough resources.',
      summary: 'Earn 100 resources before upgrading.',
    })
    expect(getUpgradeStatusCopy('already-purchased')).toEqual({
      status: 'Upgrade already installed.',
      summary: 'Weapon Power is already upgraded.',
    })
    expect(getUpgradeStatusCopy('not-preparation')).toEqual({
      status: 'Upgrade available in hangar.',
      summary: 'Return to preparation before upgrading.',
    })
    expect(getUpgradeStatusCopy('write-error')).toEqual({
      status: 'Upgrade not saved.',
      summary: 'No resources were spent. Check browser storage and try again.',
    })
    expect(getUpgradeStatusCopy('storage-unavailable')).toEqual({
      status: 'Upgrade not saved.',
      summary: 'No resources were spent. Check browser storage and try again.',
    })
    expect(getUpgradeStatusCopy('invalid-definition')).toEqual({
      status: 'Upgrade unavailable.',
      summary: 'Current upgrade data could not be applied.',
    })
    expect(getUpgradeStatusCopy('invalid-state')).toEqual({
      status: 'Upgrade unavailable.',
      summary: 'Current upgrade data could not be applied.',
    })
  })

  it('does not leak internal upgrade failure reason', () => {
    hudController.render(createState('preparation'), false, {
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
    expect(textSurface).not.toContain('invalid-definition')
    expect(textSurface).not.toContain('invalid-state')
    expect(textSurface).not.toContain('write-error')
    expect(textSurface).not.toContain('storage-unavailable')
    expect(textSurface).toContain('Not enough resources.')
    expect(textSurface).toContain('Earn 100 resources before upgrading.')
  })
})
