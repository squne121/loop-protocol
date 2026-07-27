import { describe, expect, it } from 'vitest'

import {
  LOOP_PHASE_TRANSITIONS,
  resolvePhaseTransition,
  type PhaseTransitionIntent,
} from '../src/systems/PhaseTransitionSystem'

describe('PhaseTransitionSystem', () => {
  it('lists the allowed transitions for each LoopPhase intent map', () => {
    expect(LOOP_PHASE_TRANSITIONS).toEqual({
      title_menu: {
        new_game: 'preparation',
        open_load_menu: 'load_menu',
      },
      load_menu: {
        back_to_title: 'title_menu',
        back_to_preparation: 'preparation',
        load_success: 'preparation',
      },
      preparation: {
        bootstrap_title_menu: 'title_menu',
        open_load_menu: 'load_menu',
        save_progress: 'preparation',
        reset_sortie: 'preparation',
        start_sortie: 'running',
        purchase_upgrade: 'preparation',
      },
      running: {
        sortie_terminal: 'result',
      },
      result: {
        confirm_result: 'preparation',
      },
      debrief_pending_reward: {
        legacy_claim_reward: 'debrief_reward_claimed',
      },
      debrief_reward_claimed: {
        legacy_next_sortie: 'preparation',
      },
    })
  })

  it('GIVEN legal phase and intent WHEN resolvePhaseTransition is called THEN returns ok with from/to intent payload', () => {
    expect(resolvePhaseTransition('title_menu', 'new_game')).toEqual({
      ok: true,
      from: 'title_menu',
      to: 'preparation',
      intent: 'new_game',
    })
    expect(resolvePhaseTransition('preparation', 'start_sortie')).toEqual({
      ok: true,
      from: 'preparation',
      to: 'running',
      intent: 'start_sortie',
    })
    expect(resolvePhaseTransition('debrief_pending_reward', 'legacy_claim_reward')).toEqual({
      ok: true,
      from: 'debrief_pending_reward',
      to: 'debrief_reward_claimed',
      intent: 'legacy_claim_reward',
    })
    expect(resolvePhaseTransition('debrief_reward_claimed', 'legacy_next_sortie')).toEqual({
      ok: true,
      from: 'debrief_reward_claimed',
      to: 'preparation',
      intent: 'legacy_next_sortie',
    })
    expect(resolvePhaseTransition('preparation', 'bootstrap_title_menu')).toEqual({
      ok: true,
      from: 'preparation',
      to: 'title_menu',
      intent: 'bootstrap_title_menu',
    })
  })

  // Issue #1374 (AC8): preparation can open load_menu directly, and load_menu
  // opened from preparation returns to preparation via back_to_preparation.
  it('GIVEN preparation WHEN resolving open_load_menu intent THEN transitions to load_menu', () => {
    expect(resolvePhaseTransition('preparation', 'open_load_menu')).toEqual({
      ok: true,
      from: 'preparation',
      to: 'load_menu',
      intent: 'open_load_menu',
    })
  })

  it('GIVEN load_menu WHEN resolving back_to_preparation intent THEN transitions to preparation', () => {
    expect(resolvePhaseTransition('load_menu', 'back_to_preparation')).toEqual({
      ok: true,
      from: 'load_menu',
      to: 'preparation',
      intent: 'back_to_preparation',
    })
  })

  it('GIVEN load_menu WHEN resolving back_to_title intent THEN transitions to title_menu (title_menu origin)', () => {
    expect(resolvePhaseTransition('load_menu', 'back_to_title')).toEqual({
      ok: true,
      from: 'load_menu',
      to: 'title_menu',
      intent: 'back_to_title',
    })
  })

  it('GIVEN illegal phase and intent WHEN resolvePhaseTransition is called THEN returns illegal-transition with intent', () => {
    const invalidCases: Array<{ from: Parameters<typeof resolvePhaseTransition>[0]; intent: PhaseTransitionIntent }> = [
      { from: 'running', intent: 'reset_sortie' },
      { from: 'result', intent: 'start_sortie' },
      { from: 'debrief_pending_reward', intent: 'confirm_result' },
      { from: 'title_menu', intent: 'bootstrap_title_menu' },
      { from: 'title_menu', intent: 'back_to_preparation' },
    ]

    for (const invalidCase of invalidCases) {
      expect(resolvePhaseTransition(invalidCase.from, invalidCase.intent)).toEqual({
        ok: false,
        error: {
          code: 'illegal-transition',
          from: invalidCase.from,
          intent: invalidCase.intent,
        },
      })
    }
  })
})
