import type { LoopPhase } from '../state'

type TransitionError = {
  code: 'illegal-transition'
  from: LoopPhase
  to: LoopPhase
}

export type PhaseTransitionResult =
  | { ok: true; nextPhase: LoopPhase }
  | { ok: false; error: TransitionError }

export const LOOP_PHASE_TRANSITIONS = {
  title_menu: ['load_menu', 'preparation'],
  load_menu: ['title_menu', 'preparation'],
  preparation: ['running'],
  running: ['result'],
  result: ['preparation'],
  debrief_pending_reward: ['debrief_reward_claimed'],
  debrief_reward_claimed: ['preparation'],
} as const satisfies Record<LoopPhase, readonly LoopPhase[]>

/**
 * Resolves a requested LoopPhase transition against a strict transition table.
 */
export function resolvePhaseTransition(
  currentPhase: LoopPhase,
  requestedPhase: LoopPhase,
): PhaseTransitionResult {
  if (currentPhase === requestedPhase) {
    return { ok: true, nextPhase: requestedPhase }
  }

  const allowedNextPhases = LOOP_PHASE_TRANSITIONS[currentPhase] as readonly LoopPhase[]
  if (allowedNextPhases.includes(requestedPhase)) {
    return { ok: true, nextPhase: requestedPhase }
  }

  return {
    ok: false,
    error: {
      code: 'illegal-transition',
      from: currentPhase,
      to: requestedPhase,
    },
  }
}
