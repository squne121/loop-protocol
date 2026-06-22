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

export interface PlaytestEvidenceRuntimeState {
  currentSortieId: string | null
  nextSortieSequence: number
  nextCommandSequence: number
}

type RuntimeStore = {
  sortieId: string
  terminalState: AssistPlayerTerminalState
  deterministicEvents: AssistPlayerPlaytestEvent[]
  qualitativeNotes?: QualitativeNotes
}

const DEFAULT_SORTIE_ID = 'sortie-uninitialized'

let runtimeStore: RuntimeStore = {
  sortieId: DEFAULT_SORTIE_ID,
  terminalState: 'running',
  deterministicEvents: [],
}

export function createPlaytestEvidenceRuntimeState(): PlaytestEvidenceRuntimeState {
  return {
    currentSortieId: null,
    nextSortieSequence: 1,
    nextCommandSequence: 1,
  }
}

export function beginPlaytestEvidenceSortie(
  runtime: PlaytestEvidenceRuntimeState,
): string {
  const sortieId = `sortie-${runtime.nextSortieSequence}`
  runtime.currentSortieId = sortieId
  runtime.nextSortieSequence += 1
  runtime.nextCommandSequence = 1
  runtimeStore = {
    sortieId,
    terminalState: 'running',
    deterministicEvents: [],
  }
  return sortieId
}

export function nextPlaytestCommandSequence(
  runtime: PlaytestEvidenceRuntimeState,
): number {
  const current = runtime.nextCommandSequence
  runtime.nextCommandSequence += 1
  return current
}

function ensureSortieId(sortieId: string | null | undefined): string {
  return sortieId ?? runtimeStore.sortieId ?? DEFAULT_SORTIE_ID
}

export function resetPlaytestEvidenceStore(): void {
  runtimeStore = {
    sortieId: DEFAULT_SORTIE_ID,
    terminalState: 'running',
    deterministicEvents: [],
  }
}

export function recordCommandUse(
  tick: number,
  commandSeq: number,
  accepted: boolean,
): void {
  runtimeStore.deterministicEvents.push({
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
  tick: number,
  commandSeq: number,
  reason: AssistPlayerNoopReason,
): void {
  runtimeStore.deterministicEvents.push({
    type: 'command_noop',
    tick,
    event_type_order: 20,
    command_seq: commandSeq,
    entity_id: 'player-alpha',
    reason,
  })
}

export function recordTargetSwitch(input: {
  tick: number
  commandSeq: number
  allyId: number
  fromTargetId: string | null
  toTargetId: string
  causedByCommandIntent: boolean
}): void {
  runtimeStore.deterministicEvents.push({
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

export function recordLocalThreatSample(input: {
  tick: number
  commandSeq: number
  phase: 'before' | 'after'
  threatCount: number
}): void {
  runtimeStore.deterministicEvents.push({
    type: 'local_threat_sample',
    tick: input.tick,
    event_type_order: 40,
    command_seq: input.commandSeq,
    entity_id: 'player-alpha',
    phase: input.phase,
    threat_count: input.threatCount,
  })
}

export function recordAllySurvival(input: {
  tick: number
  commandSeq: number
  sortieId: string | null
  alliesSpawned: number
  alliesSurvived: number
  protectedZoneStable: boolean
}): void {
  runtimeStore.deterministicEvents.push({
    type: 'ally_survival',
    tick: input.tick,
    event_type_order: 50,
    command_seq: input.commandSeq,
    entity_id: 'sortie-summary',
    sortie_id: ensureSortieId(input.sortieId),
    allies_spawned: input.alliesSpawned,
    allies_survived: input.alliesSurvived,
    protected_zone_stable: input.protectedZoneStable,
  })
}

export function markTerminalState(state: AssistPlayerTerminalState): void {
  runtimeStore.terminalState = state
}

export function setSelfExplanationPrompt(prompt: string): void {
  runtimeStore.qualitativeNotes = {
    self_explanation_prompt: prompt,
    self_explanation_response: runtimeStore.qualitativeNotes?.self_explanation_response,
  }
}

export function setSelfExplanationResponse(response: string): void {
  if (!runtimeStore.qualitativeNotes) {
    runtimeStore.qualitativeNotes = {
      self_explanation_prompt: 'What changed the battle outcome most, and why?',
    }
  }

  runtimeStore.qualitativeNotes = {
    ...runtimeStore.qualitativeNotes,
    self_explanation_response: response,
  }
}

export function clearSelfExplanationResponse(): void {
  if (!runtimeStore.qualitativeNotes) {
    return
  }

  runtimeStore.qualitativeNotes = {
    self_explanation_prompt: runtimeStore.qualitativeNotes.self_explanation_prompt,
  }
}

function compareOptionalString(a: string, b: string): number {
  if (a < b) return -1
  if (a > b) return 1
  return 0
}

export function snapshotDeterministicEvents(): AssistPlayerPlaytestEvent[] {
  return runtimeStore.deterministicEvents
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

export function getPlaytestEvidenceSnapshot(): AssistPlayerRuntimeEvidenceSnapshot {
  return {
    sortie_id: runtimeStore.sortieId,
    terminal_state: runtimeStore.terminalState,
    deterministic_events: snapshotDeterministicEvents(),
    qualitative_notes: runtimeStore.qualitativeNotes,
  }
}
