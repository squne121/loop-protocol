import type { LoopPhase } from '../state'

export type PhaseTransitionIntent =
  | 'bootstrap_title_menu'
  | 'new_game'
  | 'open_load_menu'
  | 'back_to_title'
  | 'load_success'
  | 'save_progress'
  | 'reset_sortie'
  | 'start_sortie'
  | 'sortie_terminal'
  | 'confirm_result'
  | 'legacy_claim_reward'
  | 'legacy_next_sortie'

type TransitionError = {
  code: 'illegal-transition'
  from: LoopPhase
  intent: PhaseTransitionIntent
}

export type PhaseTransitionResult =
  | { ok: true; from: LoopPhase; to: LoopPhase; intent: PhaseTransitionIntent }
  | { ok: false; error: TransitionError }

export const LOOP_PHASE_TRANSITIONS = {
  title_menu: {
    new_game: 'preparation',
    open_load_menu: 'load_menu',
  },
  load_menu: {
    back_to_title: 'title_menu',
    load_success: 'preparation',
  },
  preparation: {
    bootstrap_title_menu: 'title_menu',
    save_progress: 'preparation',
    reset_sortie: 'preparation',
    start_sortie: 'running',
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
} as const satisfies Record<
  LoopPhase,
  Partial<Record<PhaseTransitionIntent, LoopPhase>>
>

export function resolvePhaseTransition(
  currentPhase: LoopPhase,
  intent: PhaseTransitionIntent,
): PhaseTransitionResult {
  const transitionMap =
    LOOP_PHASE_TRANSITIONS[currentPhase] as Partial<Record<PhaseTransitionIntent, LoopPhase>>
  const nextPhase = transitionMap[intent]

  if (nextPhase) {
    return {
      ok: true,
      from: currentPhase,
      to: nextPhase,
      intent,
    }
  }

  return {
    ok: false,
    error: {
      code: 'illegal-transition',
      from: currentPhase,
      intent,
    },
  }
}
