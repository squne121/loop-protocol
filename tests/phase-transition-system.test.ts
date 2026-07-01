import { describe, expect, it } from 'vitest'

import {
  applyBootstrapLoopPhaseTransition,
  isBootstrapTransitionAllowed,
  LOOP_PHASE_TRANSITIONS,
  resolvePhaseTransition,
} from '../src/systems/PhaseTransitionSystem'
import { createInitialGameState } from '../src/state'

describe('PhaseTransitionSystem', () => {
  it('lists the allowed transitions for each LoopPhase', () => {
    expect(LOOP_PHASE_TRANSITIONS).toEqual({
      title_menu: ['load_menu', 'preparation'],
      load_menu: ['title_menu', 'preparation'],
      preparation: ['running'],
      running: ['result'],
      result: ['preparation'],
      debrief_pending_reward: ['debrief_reward_claimed'],
      debrief_reward_claimed: ['preparation'],
    })
  })

  it('GIVEN legal phase pair WHEN resolvePhaseTransition is called THEN returns ok and nextPhase', () => {
    expect(resolvePhaseTransition('title_menu', 'preparation')).toEqual({
      ok: true,
      nextPhase: 'preparation',
    })
    expect(resolvePhaseTransition('preparation', 'running')).toEqual({
      ok: true,
      nextPhase: 'running',
    })
    expect(resolvePhaseTransition('debrief_pending_reward', 'debrief_reward_claimed')).toEqual({
      ok: true,
      nextPhase: 'debrief_reward_claimed',
    })
    expect(resolvePhaseTransition('debrief_reward_claimed', 'preparation')).toEqual({
      ok: true,
      nextPhase: 'preparation',
    })
  })

  it('GIVEN illegal phase pair WHEN resolvePhaseTransition is called THEN returns illegal-transition', () => {
    expect(resolvePhaseTransition('running', 'preparation')).toEqual({
      ok: false,
      error: { code: 'illegal-transition', from: 'running', to: 'preparation' },
    })
    expect(resolvePhaseTransition('result', 'running')).toEqual({
      ok: false,
      error: { code: 'illegal-transition', from: 'result', to: 'running' },
    })
    expect(resolvePhaseTransition('debrief_pending_reward', 'preparation')).toEqual({
      ok: false,
      error: { code: 'illegal-transition', from: 'debrief_pending_reward', to: 'preparation' },
    })
  })

  it('GIVEN identical phase pair WHEN resolvePhaseTransition is called THEN resolves as no-op success', () => {
    expect(resolvePhaseTransition('result', 'result')).toEqual({
      ok: true,
      nextPhase: 'result',
    })
  })

  it('GIVEN default bootstrap state WHEN title_menu transition is applied THEN transition is allowed and state moves to title_menu', () => {
    const state = createInitialGameState()

    expect(isBootstrapTransitionAllowed(state.loopPhase, 'title_menu')).toBe(true)
    applyBootstrapLoopPhaseTransition(state, 'title_menu')

    expect(state.loopPhase).toBe('title_menu')
  })

  it('GIVEN default bootstrap state WHEN invalid transition is applied THEN helper throws', () => {
    const state = createInitialGameState()

    expect(isBootstrapTransitionAllowed(state.loopPhase, 'running')).toBe(false)
    expect(() => applyBootstrapLoopPhaseTransition(state, 'running')).toThrow(
      'Invalid bootstrap loop-phase transition: preparation -> running',
    )
  })
})
