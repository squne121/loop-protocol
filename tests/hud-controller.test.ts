/**
 * @vitest-environment jsdom
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createHudController } from '../src/ui/HudController'
import {
  runNextSortieHandler,
  runConfirmResultHandler,
} from '../src/main'
import type { GameState, LoopPhase, ResultRewardStatus, SortieResult } from '../src/state'
import { createGameSnapshot } from '../src/state'

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

    expect(container.querySelector('[data-field="loop-phase"]')?.textContent).toBe('Preparation')
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

    expect(container.querySelector('[data-field="loop-phase"]')?.textContent).toBe('Title Menu')
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

    expect(container.querySelector('[data-field="loop-phase"]')?.textContent).toBe('Load Menu')
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

    expect(container.querySelector('[data-field="loop-phase"]')?.textContent).toBe('Sortie running')
    expect(queryButton(container, 'start-sortie').disabled).toBe(true)
    expect(queryButton(container, 'claim-reward').disabled).toBe(true)
    expect(queryButton(container, 'confirm-result').disabled).toBe(true)
    expect(queryButton(container, 'next-sortie').disabled).toBe(true)
    expect(queryButton(container, 'save').disabled).toBe(true)
    expect(queryButton(container, 'load-game').disabled).toBe(true)
    expect(queryButton(container, 'reset').disabled).toBe(true)
  })

  it('GIVEN result phase with pending reward WHEN render called THEN confirm-result enabled, claim-reward disabled (AC4, AC5)', () => {
    hudController.render(createState('result', 'pending'), false)

    expect(container.querySelector('[data-field="loop-phase"]')?.textContent).toBe('Result')
    expect(container.querySelector('[data-field="sortie-status"]')?.textContent).toBe('Victory')
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

    expect(container.querySelector('[data-field="loop-phase"]')?.textContent).toBe('Result')
    expect(queryButton(container, 'confirm-result').disabled).toBe(false)
    expect(queryButton(container, 'claim-reward').disabled).toBe(true)
    expect(queryButton(container, 'start-sortie').disabled).toBe(true)
    expect(queryButton(container, 'save').disabled).toBe(true)
  })

  it('GIVEN debrief_pending_reward WHEN render called THEN Debrief: reward pending enables Claim reward only', () => {
    hudController.render(createState('debrief_pending_reward'), false)

    expect(container.querySelector('[data-field="loop-phase"]')?.textContent).toBe('Debrief: reward pending')
    expect(container.querySelector('[data-field="sortie-status"]')?.textContent).toBe('Victory')
    expect(container.querySelector('[data-field="sortie-result"]')?.textContent).toBe('Victory')
    expect(queryButton(container, 'claim-reward').disabled).toBe(false)
    expect(queryButton(container, 'start-sortie').disabled).toBe(true)
    expect(queryButton(container, 'next-sortie').disabled).toBe(true)
    expect(queryButton(container, 'save').disabled).toBe(true)
    expect(queryButton(container, 'load-game').disabled).toBe(true)
    expect(queryButton(container, 'reset').disabled).toBe(true)
  })

  it('GIVEN debrief_reward_claimed WHEN render called THEN Debrief: reward claimed enables Next sortie only', () => {
    hudController.render(createState('debrief_reward_claimed'), false)

    expect(container.querySelector('[data-field="loop-phase"]')?.textContent).toBe('Debrief: reward claimed')
    expect(container.querySelector('[data-field="sortie-status"]')?.textContent).toBe('Victory')
    expect(container.querySelector('[data-field="sortie-result"]')?.textContent).toBe('Victory')
    expect(queryButton(container, 'next-sortie').disabled).toBe(false)
    expect(queryButton(container, 'start-sortie').disabled).toBe(true)
    expect(queryButton(container, 'claim-reward').disabled).toBe(true)
    expect(queryButton(container, 'save').disabled).toBe(true)
    expect(queryButton(container, 'load-game').disabled).toBe(true)
    expect(queryButton(container, 'reset').disabled).toBe(true)
  })

  it('GIVEN feedback copy WHEN render called THEN status region exposes claim and persistence copy without innerHTML', () => {
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

    expect(actions.onNewGame).not.toHaveBeenCalled()
    expect(actions.onStartSortie).not.toHaveBeenCalled()
    expect(actions.onClaimReward).not.toHaveBeenCalled()
    expect(actions.onConfirmResult).not.toHaveBeenCalled()
    expect(actions.onNextSortie).not.toHaveBeenCalled()
    expect(actions.onSave).not.toHaveBeenCalled()
    expect(actions.onLoadGame).not.toHaveBeenCalled()
    expect(actions.onReset).toHaveBeenCalledTimes(0)
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
  let hudController: ReturnType<typeof createHudController>

  beforeEach(() => {
    container = document.createElement('div')
    hudController = createHudController(container, {
      onNewGame: vi.fn(),
      onStartSortie: vi.fn(),
      onClaimReward: vi.fn(),
      onConfirmResult: vi.fn(),
      onNextSortie: vi.fn(),
      onSave: vi.fn(),
      onLoadGame: vi.fn(),
      onReset: vi.fn(),
      canLoadGame: vi.fn(() => true),
      onTogglePause: vi.fn(),
    })
  })

  it('AC1: GIVEN debrief_reward_claimed WHEN next-sortie click via runNextSortieHandler THEN HUD shows "Returned to preparation." / "Use Start sortie to begin the next sortie."', () => {
    const state = createState('debrief_reward_claimed')
    hudController.render(state, false)

    function renderHudAfterAction() {
      hudController.render(state, false)
    }

    const button = queryButton(container, 'next-sortie')
    expect(button.disabled).toBe(false)

    button.addEventListener('click', () => {
      runNextSortieHandler(state, {
        setHudFeedback: (status, summary) => {
          state.telemetry.status = status
          state.telemetry.lastCommandSummary = summary
        },
      })
      renderHudAfterAction()
    })

    button.click()

    expect(state.loopPhase).toBe('preparation')
    expect(container.querySelector('[data-field="status"]')?.textContent).toBe('Returned to preparation.')
    expect(container.querySelector('[data-field="command"]')?.textContent).toBe('Use Start sortie to begin the next sortie.')
  })

  it('AC2-AC3: GIVEN result + pending reward WHEN confirm-result click via runConfirmResultHandler with fake save success THEN HUD shows "Result confirmed." / "Progress saved locally." and fakeProgressionStorageSave called exactly once', () => {
    const state = createState('result', 'pending')
    hudController.render(state, false)

    function renderHudAfterAction() {
      hudController.render(state, false)
    }

    const fakeProgressionStorageSave = vi.fn(() => ({ ok: true as const }))

    const button = queryButton(container, 'confirm-result')
    expect(button.disabled).toBe(false)

    button.addEventListener('click', () => {
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

    button.click()

    expect(state.loopPhase).toBe('preparation')
    expect(container.querySelector('[data-field="status"]')?.textContent).toBe('Result confirmed.')
    expect(container.querySelector('[data-field="command"]')?.textContent).toBe('Progress saved locally.')
    expect(fakeProgressionStorageSave).toHaveBeenCalledTimes(1)
  })
})
