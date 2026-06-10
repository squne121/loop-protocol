/**
 * @vitest-environment jsdom
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createHudController } from '../src/ui/HudController'
import type { GameState, LoopPhase, SortieResult } from '../src/state'

const TERMINAL_SORTIE_RESULT = {
  outcome: 'victory',
  endReason: 'all_enemies_defeated',
  durationMs: 30_000,
  kills: 4,
  shotsFired: 18,
  playerHpRemaining: 6,
} satisfies SortieResult

function createState(loopPhase: LoopPhase = 'preparation'): GameState {
  const isDebrief = loopPhase === 'debrief_pending_reward' || loopPhase === 'debrief_reward_claimed'

  return {
    tick: 0,
    elapsedMs: 0,
    loopPhase,
    pendingRewardApplicationId: isDebrief ? 'sortie-reward-1' : null,
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
        : isDebrief
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
    onStartSortie: ReturnType<typeof vi.fn>
    onClaimReward: ReturnType<typeof vi.fn>
    onNextSortie: ReturnType<typeof vi.fn>
    onQuickSave: ReturnType<typeof vi.fn>
    onQuickLoad: ReturnType<typeof vi.fn>
    onReset: ReturnType<typeof vi.fn>
    canQuickLoad: ReturnType<typeof vi.fn>
  }
  let hudController: ReturnType<typeof createHudController>

  beforeEach(() => {
    container = document.createElement('div')
    actions = {
      onStartSortie: vi.fn(),
      onClaimReward: vi.fn(),
      onNextSortie: vi.fn(),
      onQuickSave: vi.fn(),
      onQuickLoad: vi.fn(),
      onReset: vi.fn(),
      canQuickLoad: vi.fn(() => true),
    }
    hudController = createHudController(container, actions)
  })

  it('GIVEN preparation WHEN render called THEN phase copy and preparation actions are enabled', () => {
    hudController.render(createState('preparation'))

    expect(container.querySelector('[data-field="loop-phase"]')?.textContent).toBe('Preparation')
    expect(queryButton(container, 'start-sortie').disabled).toBe(false)
    expect(queryButton(container, 'quick-save').disabled).toBe(false)
    expect(queryButton(container, 'quick-load').disabled).toBe(false)
    expect(queryButton(container, 'reset').disabled).toBe(false)
    expect(queryButton(container, 'claim-reward').disabled).toBe(true)
    expect(queryButton(container, 'next-sortie').disabled).toBe(true)
    expect(queryButton(container, 'reset').getAttribute('title')).toContain('destructive boundary')
  })

  it('GIVEN preparation without a loadable snapshot WHEN render called THEN Quick Load is disabled', () => {
    actions.canQuickLoad.mockReturnValue(false)

    hudController.render(createState('preparation'))

    expect(queryButton(container, 'quick-load').disabled).toBe(true)
  })

  it('GIVEN running WHEN render called THEN button.disabled marks the full action surface as disabled', () => {
    hudController.render(createState('running'))

    expect(container.querySelector('[data-field="loop-phase"]')?.textContent).toBe('Sortie running')
    expect(queryButton(container, 'start-sortie').disabled).toBe(true)
    expect(queryButton(container, 'claim-reward').disabled).toBe(true)
    expect(queryButton(container, 'next-sortie').disabled).toBe(true)
    expect(queryButton(container, 'quick-save').disabled).toBe(true)
    expect(queryButton(container, 'quick-load').disabled).toBe(true)
    expect(queryButton(container, 'reset').disabled).toBe(true)
  })

  it('GIVEN debrief_pending_reward WHEN render called THEN Debrief: reward pending enables Claim reward only', () => {
    hudController.render(createState('debrief_pending_reward'))

    expect(container.querySelector('[data-field="loop-phase"]')?.textContent).toBe('Debrief: reward pending')
    expect(container.querySelector('[data-field="sortie-status"]')?.textContent).toBe('Victory')
    expect(container.querySelector('[data-field="sortie-result"]')?.textContent).toBe('Victory')
    expect(queryButton(container, 'claim-reward').disabled).toBe(false)
    expect(queryButton(container, 'start-sortie').disabled).toBe(true)
    expect(queryButton(container, 'next-sortie').disabled).toBe(true)
    expect(queryButton(container, 'quick-save').disabled).toBe(true)
    expect(queryButton(container, 'quick-load').disabled).toBe(true)
    expect(queryButton(container, 'reset').disabled).toBe(true)
  })

  it('GIVEN debrief_reward_claimed WHEN render called THEN Debrief: reward claimed enables Next sortie only', () => {
    hudController.render(createState('debrief_reward_claimed'))

    expect(container.querySelector('[data-field="loop-phase"]')?.textContent).toBe('Debrief: reward claimed')
    expect(container.querySelector('[data-field="sortie-status"]')?.textContent).toBe('Victory')
    expect(container.querySelector('[data-field="sortie-result"]')?.textContent).toBe('Victory')
    expect(queryButton(container, 'next-sortie').disabled).toBe(false)
    expect(queryButton(container, 'start-sortie').disabled).toBe(true)
    expect(queryButton(container, 'claim-reward').disabled).toBe(true)
    expect(queryButton(container, 'quick-save').disabled).toBe(true)
    expect(queryButton(container, 'quick-load').disabled).toBe(true)
    expect(queryButton(container, 'reset').disabled).toBe(true)
  })

  it('GIVEN feedback copy WHEN render called THEN status region exposes claim and persistence copy without innerHTML', () => {
    const state = createState('debrief_reward_claimed')
    state.telemetry.status = 'Reward claimed for this session.'
    state.telemetry.lastCommandSummary = 'Persistence will be handled by issue #739.'

    hudController.render(state)

    const status = container.querySelector('[data-field="status"]')
    expect(status?.textContent).toBe('Reward claimed for this session.')
    expect(status?.getAttribute('role')).toBe('status')
    expect(status?.getAttribute('aria-live')).toBe('polite')
    expect(container.querySelector('[data-field="command"]')?.textContent).toBe(
      'Persistence will be handled by issue #739.',
    )
  })

  it('GIVEN running WHEN disabled buttons are clicked THEN callbacks are not invoked', () => {
    hudController.render(createState('running'))

    queryButton(container, 'start-sortie').click()
    queryButton(container, 'claim-reward').click()
    queryButton(container, 'next-sortie').click()
    queryButton(container, 'quick-save').click()
    queryButton(container, 'quick-load').click()
    queryButton(container, 'reset').click()

    expect(actions.onStartSortie).not.toHaveBeenCalled()
    expect(actions.onClaimReward).not.toHaveBeenCalled()
    expect(actions.onNextSortie).not.toHaveBeenCalled()
    expect(actions.onQuickSave).not.toHaveBeenCalled()
    expect(actions.onQuickLoad).not.toHaveBeenCalled()
    expect(actions.onReset).toHaveBeenCalledTimes(0)
  })

  it('GIVEN debrief_reward_claimed WHEN disabled claim button is clicked THEN claim callback remains a no-op surface', () => {
    hudController.render(createState('debrief_reward_claimed'))

    queryButton(container, 'claim-reward').click()
    queryButton(container, 'next-sortie').click()

    expect(actions.onClaimReward).not.toHaveBeenCalled()
    expect(actions.onNextSortie).toHaveBeenCalledTimes(1)
  })
})
