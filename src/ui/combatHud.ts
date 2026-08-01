import type { GameState } from '../state'
import { formatCombatNumber } from '../render/renderUtils'

/**
 * Combat HUD (Issue #1375): the compact, running-only player-facing surface
 * rendered inside `data-combat-hud` (`src/ui/HudController.ts` owns DOM
 * creation and phase routing; this module owns the DOM markup fragment and
 * the view model formatter — the single translation boundary between raw
 * `GameState` and the six combat HUD fields: Hull, Kills, Elapsed, Weapon
 * readiness, Assist allies, Pause).
 *
 * Deliberately excludes: Mission phase/status/outcome, Pilot updates, raw
 * telemetry, Collect payout, Return to hangar, Prepare next sortie (AC2) —
 * those remain on the temporary `data-legacy-result-surface` compatibility
 * root owned by `HudController`.
 */

/** Player-facing weapon readiness state (AC2: no raw `weaponCooldownMs`). */
export type CombatHudWeaponState = 'ready' | 'recharging'

/**
 * View model for the combat HUD (AC4, AC6). `elapsedLabel` is derived from
 * `elapsedTicks * activeFixedDeltaMs` (never `/ 60`, wall-clock, or rAF
 * time) so the running timer is deterministic under E2E/VRT fixtures.
 */
export interface CombatHudViewModel {
  hullLabel: string
  kills: number
  elapsedLabel: string
  weaponState: CombatHudWeaponState
  weaponLabel: string
  assistStatus: string
  assistDisabled: boolean
  paused: boolean
  pauseDisabled: boolean
}

/** Player-facing assist availability copy (moved from HudController, Issue #1375). */
export function getCombatHudAssistStatusCopy(state: GameState): string {
  if (state.loopPhase !== 'running' || state.sortie.status !== 'running') {
    return 'Assist is available during sortie.'
  }

  if (state.allies.length === 0) {
    return 'No ally available.'
  }

  const hasLivingEnemy = state.enemies.some((enemy) => !enemy.defeated)
  const hasAssignedTarget = state.allies.some((ally) => ally.targetEntityId !== null)

  if (state.commandIntentRuntime.activeIntent === 'assist_player') {
    if (hasAssignedTarget) {
      return 'Allies covering you.'
    }
    return hasLivingEnemy ? 'Assist signal sent.' : 'No target to assist.'
  }

  return hasLivingEnemy ? 'Assist ready.' : 'No target to assist.'
}

/**
 * Derives the running-timer label from `elapsedTicks * activeFixedDeltaMs`
 * (AC4). `activeFixedDeltaMs` is a runtime-local value supplied by the
 * caller (`src/main.ts`) — never read from `GameState` (Out of Scope:
 * `GameState.sortie` does not carry `fixedDeltaMs`).
 */
export function formatCombatHudElapsedLabel(elapsedTicks: number, activeFixedDeltaMs: number): string {
  const elapsedSeconds = (elapsedTicks * activeFixedDeltaMs) / 1000
  return `${elapsedSeconds.toFixed(1)}s`
}

/**
 * Builds the combat HUD view model (AC2, AC4, AC6). Pure function of
 * `state` + `isPaused` + `activeFixedDeltaMs` — never mutates `GameState`
 * (`src/ui` is read-only over `src/state`, per `src/ui/CLAUDE.md`).
 */
export function buildCombatHudViewModel(
  state: GameState,
  isPaused: boolean,
  activeFixedDeltaMs: number,
): CombatHudViewModel {
  const kills =
    state.sortie.result !== null
      ? state.sortie.result.kills
      : state.enemies.filter((enemy) => enemy.defeated).length

  const weaponState: CombatHudWeaponState = state.player.weaponCooldownMs <= 0 ? 'ready' : 'recharging'

  return {
    hullLabel: `${formatCombatNumber(state.player.hp)}/${formatCombatNumber(state.player.maxHp)}`,
    kills,
    elapsedLabel: formatCombatHudElapsedLabel(state.sortie.elapsedTicks, activeFixedDeltaMs),
    weaponState,
    weaponLabel: weaponState === 'ready' ? 'Ready' : 'Recharging',
    assistStatus: getCombatHudAssistStatusCopy(state),
    assistDisabled: state.loopPhase !== 'running',
    paused: isPaused,
    // BLOCKER 1 parity (Issue #1374): pause remains togglable to resume even
    // if loopPhase has already left 'running' (rare timing edge), but entry
    // requires 'running'.
    pauseDisabled: state.loopPhase !== 'running' && !isPaused,
  }
}

/**
 * The combat HUD DOM fragment (AC1, AC2, AC3, AC6). Consumed by
 * `HudController.ts`'s `createHudController()`, which composes it alongside
 * the legacy result/debrief surface and owns phase routing / visibility.
 */
export const COMBAT_HUD_MARKUP = `
  <section class="panel combat-hud" data-combat-hud hidden inert>
    <p class="eyebrow">Combat</p>
    <dl class="stat-grid">
      <div><dt>Hull</dt><dd data-field="combat-hud-hull"></dd></div>
      <div><dt>Kills</dt><dd data-field="combat-hud-kills"></dd></div>
      <div><dt>Elapsed</dt><dd data-field="combat-hud-elapsed"></dd></div>
      <div><dt>Weapon</dt><dd data-field="combat-hud-weapon"></dd></div>
    </dl>
    <button
      type="button"
      data-action="assist-player"
      data-battle-interactive="true"
      aria-label="Assist allies"
    >Assist allies</button>
    <p
      class="status-copy"
      data-field="combat-hud-assist-status"
      role="status"
      aria-live="polite"
      aria-atomic="true"
    ></p>
    <button
      type="button"
      data-action="toggle-pause"
      data-battle-interactive="true"
      aria-pressed="false"
      title="Pause or resume simulation. Also toggled by Escape."
    >Pause</button>
  </section>
`
