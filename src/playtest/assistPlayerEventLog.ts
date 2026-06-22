export type AssistPlayerNoopReason =
  | 'no_ally'
  | 'no_target'
  | 'not_combat'
  | 'expired'

export type AssistPlayerTerminalState =
  | 'running'
  | 'victory'
  | 'defeat'
  | 'timeout'

export type AssistPlayerPlaytestEvent =
  | {
      type: 'command_use'
      tick: number
      event_type_order: 10
      command_seq: number
      entity_id: 'player-alpha'
      intent: 'assist_player'
      accepted: boolean
    }
  | {
      type: 'command_noop'
      tick: number
      event_type_order: 20
      command_seq: number
      entity_id: 'player-alpha'
      reason: AssistPlayerNoopReason
    }
  | {
      type: 'target_switch'
      tick: number
      event_type_order: 30
      command_seq: number
      entity_id: `ally:${number}`
      ally_id: number
      from_target_id: string | null
      to_target_id: string
      caused_by_command_intent: boolean
    }
  | {
      type: 'local_threat_sample'
      tick: number
      event_type_order: 40
      command_seq: number
      entity_id: 'player-alpha'
      phase: 'before' | 'after'
      threat_count: number
    }
  | {
      type: 'ally_survival'
      tick: number
      event_type_order: 50
      command_seq: number
      entity_id: 'sortie-summary'
      sortie_id: string
      allies_spawned: number
      allies_survived: number
      protected_zone_stable: boolean
    }

export interface QualitativeNotes {
  self_explanation_prompt: string
  self_explanation_response?: string
}

export interface AssistPlayerRuntimeEvidenceSnapshot {
  sortie_id: string
  terminal_state: AssistPlayerTerminalState
  deterministic_events: AssistPlayerPlaytestEvent[]
  qualitative_notes?: QualitativeNotes
}

const DEFAULT_SORTIE_ID = 'sortie-uninitialized'

/**
 * State-scoped playtest evidence runtime (B1, Issue #987).
 *
 * The deterministic event log and qualitative notes live inside the GameState
 * (no module-global store). Every recorder / accessor below takes this state
 * object as its first argument and reads/writes the arrays it owns.
 */
export interface PlaytestEvidenceRuntimeState {
  currentSortieId: string | null
  nextSortieSequence: number
  nextCommandSequence: number
  sortieId: string
  terminalState: AssistPlayerTerminalState
  deterministicEvents: AssistPlayerPlaytestEvent[]
  qualitativeNotes?: QualitativeNotes
}

export function createPlaytestEvidenceRuntimeState(): PlaytestEvidenceRuntimeState {
  return {
    currentSortieId: null,
    nextSortieSequence: 1,
    nextCommandSequence: 1,
    sortieId: DEFAULT_SORTIE_ID,
    terminalState: 'running',
    deterministicEvents: [],
  }
}

export function beginPlaytestEvidenceSortie(
  runtime: PlaytestEvidenceRuntimeState,
): string {
  const sortieId = `sortie-${runtime.nextSortieSequence}`
  runtime.currentSortieId = sortieId
  runtime.nextSortieSequence += 1
  runtime.nextCommandSequence = 1
  runtime.sortieId = sortieId
  runtime.terminalState = 'running'
  runtime.deterministicEvents = []
  runtime.qualitativeNotes = undefined
  return sortieId
}

export function nextPlaytestCommandSequence(
  runtime: PlaytestEvidenceRuntimeState,
): number {
  const current = runtime.nextCommandSequence
  runtime.nextCommandSequence += 1
  return current
}

function ensureSortieId(
  runtime: PlaytestEvidenceRuntimeState,
  sortieId: string | null | undefined,
): string {
  return sortieId ?? runtime.sortieId ?? DEFAULT_SORTIE_ID
}

/**
 * Reset a runtime back to its uninitialized state. Used by tests to clear the
 * deterministic event log between cases.
 */
export function resetPlaytestEvidenceRuntimeState(
  runtime: PlaytestEvidenceRuntimeState,
): void {
  runtime.currentSortieId = null
  runtime.nextSortieSequence = 1
  runtime.nextCommandSequence = 1
  runtime.sortieId = DEFAULT_SORTIE_ID
  runtime.terminalState = 'running'
  runtime.deterministicEvents = []
  runtime.qualitativeNotes = undefined
}

export function recordCommandUse(
  runtime: PlaytestEvidenceRuntimeState,
  tick: number,
  commandSeq: number,
  accepted: boolean,
): void {
  runtime.deterministicEvents.push({
    type: 'command_use',
    tick,
    event_type_order: 10,
    command_seq: commandSeq,
    entity_id: 'player-alpha',
    intent: 'assist_player',
    accepted,
  })
}

export function recordCommandNoop(
  runtime: PlaytestEvidenceRuntimeState,
  tick: number,
  commandSeq: number,
  reason: AssistPlayerNoopReason,
): void {
  runtime.deterministicEvents.push({
    type: 'command_noop',
    tick,
    event_type_order: 20,
    command_seq: commandSeq,
    entity_id: 'player-alpha',
    reason,
  })
}

export function recordTargetSwitch(
  runtime: PlaytestEvidenceRuntimeState,
  input: {
    tick: number
    commandSeq: number
    allyId: number
    fromTargetId: string | null
    toTargetId: string
    causedByCommandIntent: boolean
  },
): void {
  runtime.deterministicEvents.push({
    type: 'target_switch',
    tick: input.tick,
    event_type_order: 30,
    command_seq: input.commandSeq,
    entity_id: `ally:${input.allyId}`,
    ally_id: input.allyId,
    from_target_id: input.fromTargetId,
    to_target_id: input.toTargetId,
    caused_by_command_intent: input.causedByCommandIntent,
  })
}

export function recordLocalThreatSample(
  runtime: PlaytestEvidenceRuntimeState,
  input: {
    tick: number
    commandSeq: number
    phase: 'before' | 'after'
    threatCount: number
  },
): void {
  runtime.deterministicEvents.push({
    type: 'local_threat_sample',
    tick: input.tick,
    event_type_order: 40,
    command_seq: input.commandSeq,
    entity_id: 'player-alpha',
    phase: input.phase,
    threat_count: input.threatCount,
  })
}

export function recordAllySurvival(
  runtime: PlaytestEvidenceRuntimeState,
  input: {
    tick: number
    commandSeq: number
    sortieId: string | null
    alliesSpawned: number
    alliesSurvived: number
    protectedZoneStable: boolean
  },
): void {
  runtime.deterministicEvents.push({
    type: 'ally_survival',
    tick: input.tick,
    event_type_order: 50,
    command_seq: input.commandSeq,
    entity_id: 'sortie-summary',
    sortie_id: ensureSortieId(runtime, input.sortieId),
    allies_spawned: input.alliesSpawned,
    allies_survived: input.alliesSurvived,
    protected_zone_stable: input.protectedZoneStable,
  })
}

export function markTerminalState(
  runtime: PlaytestEvidenceRuntimeState,
  state: AssistPlayerTerminalState,
): void {
  runtime.terminalState = state
}

export function setSelfExplanationPrompt(
  runtime: PlaytestEvidenceRuntimeState,
  prompt: string,
): void {
  runtime.qualitativeNotes = {
    self_explanation_prompt: prompt,
    self_explanation_response: runtime.qualitativeNotes?.self_explanation_response,
  }
}

export function setSelfExplanationResponse(
  runtime: PlaytestEvidenceRuntimeState,
  response: string,
): void {
  if (!runtime.qualitativeNotes) {
    runtime.qualitativeNotes = {
      self_explanation_prompt: 'What changed the battle outcome most, and why?',
    }
  }

  runtime.qualitativeNotes = {
    ...runtime.qualitativeNotes,
    self_explanation_response: response,
  }
}

export function clearSelfExplanationResponse(
  runtime: PlaytestEvidenceRuntimeState,
): void {
  if (!runtime.qualitativeNotes) {
    return
  }

  runtime.qualitativeNotes = {
    self_explanation_prompt: runtime.qualitativeNotes.self_explanation_prompt,
  }
}

function compareOptionalString(a: string, b: string): number {
  if (a < b) return -1
  if (a > b) return 1
  return 0
}

export function snapshotDeterministicEvents(
  runtime: PlaytestEvidenceRuntimeState,
): AssistPlayerPlaytestEvent[] {
  return runtime.deterministicEvents
    .slice()
    .sort((left, right) => {
      if (left.tick !== right.tick) {
        return left.tick - right.tick
      }
      if (left.event_type_order !== right.event_type_order) {
        return left.event_type_order - right.event_type_order
      }
      if (left.command_seq !== right.command_seq) {
        return left.command_seq - right.command_seq
      }
      return compareOptionalString(left.entity_id, right.entity_id)
    })
}

export function getPlaytestEvidenceSnapshot(
  runtime: PlaytestEvidenceRuntimeState,
): AssistPlayerRuntimeEvidenceSnapshot {
  return {
    sortie_id: runtime.sortieId,
    terminal_state: runtime.terminalState,
    deterministic_events: snapshotDeterministicEvents(runtime),
    qualitative_notes: runtime.qualitativeNotes,
  }
}
