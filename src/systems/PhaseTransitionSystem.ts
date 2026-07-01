import type { GameState, LoopPhase } from '../state'

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

const BOOTSTRAP_TRANSITION_TARGETS = {
  title_menu: ['title_menu'],
  load_menu: [],
  preparation: ['title_menu'],
  running: [],
  result: [],
  debrief_pending_reward: [],
  debrief_reward_claimed: [],
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

export function isBootstrapTransitionAllowed(
  fromPhase: LoopPhase,
  toPhase: LoopPhase,
): boolean {
  const allowedTargets = BOOTSTRAP_TRANSITION_TARGETS[fromPhase] as readonly LoopPhase[]
  return allowedTargets.includes(toPhase)
}

export function applyBootstrapLoopPhaseTransition(
  state: GameState,
  toPhase: LoopPhase,
): void {
  if (!isBootstrapTransitionAllowed(state.loopPhase, toPhase)) {
    throw new Error(`Invalid bootstrap loop-phase transition: ${state.loopPhase} -> ${toPhase}`)
  }

  state.loopPhase = toPhase
}
