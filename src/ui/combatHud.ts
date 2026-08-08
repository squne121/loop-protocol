import type { GameState } from '../state'
import { formatCombatNumber } from '../render/renderUtils'

/**
 * Combat HUD (Issue #1375, placement/safe-zone/priority redesign Issue
 * #1958): the compact, running-only player-facing surface rendered inside
 * `data-combat-hud` (`src/ui/HudController.ts` owns DOM creation and phase
 * routing; this module owns the DOM markup fragment and the view model
 * formatter — the single translation boundary between raw `GameState` and
 * the combat HUD fields: Hull/critical, Kills, Elapsed, Weapon readiness,
 * Assist allies, Pause).
 *
 * Deliberately excludes: Mission phase/status/outcome, Pilot updates, raw
 * telemetry, Collect payout, Return to hangar, Prepare next sortie (AC2) —
 * those remain on the temporary `data-legacy-result-surface` compatibility
 * root owned by `HudController`.
 */

/** Player-facing weapon readiness state (AC2: no raw `weaponCooldownMs`). */
export type CombatHudWeaponState = 'ready' | 'recharging'

/**
 * Hull ratio at/below which the persistent status cluster shows the
 * critical warning (AC6: text/shape/icon, never color-only). 0.25 chosen so
 * "critical" reliably triggers before the terminal 1-HP display floor
 * introduced by `formatCombatNumber`'s Math.ceil policy (Issue #788),
 * regardless of `maxHp`.
 */
export const COMBAT_HUD_CRITICAL_HULL_RATIO = 0.25

// ---------------------------------------------------------------------------
// AC1: Semantic state table — display condition / placement / collapse
// priority / copy for every combat HUD fragment. Fixed here as the single
// source of truth so tests (`tests/hud-controller.test.ts`) and the
// implementation (`buildCombatHudViewModel` / `COMBAT_HUD_MARKUP` below)
// cannot silently drift apart. `priority` is the *collapse order*: higher
// numbers collapse first (lowest priority) when vertical space is short
// (`docs/dev/visual-baseline-registry.md` progressive-disclosure policy
// referenced by the Issue's Current Validated Scope). HULL/critical and
// Pause never collapse (`collapsible: false`) — they are the persistent
// safety-critical surfaces.
// ---------------------------------------------------------------------------

export type CombatHudFragmentId =
  | 'hull'
  | 'critical'
  | 'elapsed'
  | 'kills'
  | 'weapon'
  | 'assist'
  | 'pause'

export interface CombatHudSemanticStateEntry {
  /** Stable fragment identity, matches `data-field`/`data-action` suffixes below. */
  id: CombatHudFragmentId
  /** Human-readable display condition (when the fragment is shown at all). */
  displayCondition: string
  /** Safe-zone placement region (Current Validated Scope). */
  placement: 'bottom-left-status' | 'top-center-low-prominence' | 'edge-control' | 'separate-pause-control'
  /** Collapse order under progressive disclosure; `false` = never collapses. */
  collapsible: number | false
  /** Player-facing copy contract (never raw internal state, AC6). */
  copy: string
}

/**
 * AC1 fixed semantic state table. Order matches collapse priority intent
 * (persistent-first); `collapsible` numeric values are the actual collapse
 * order consumed by the `@media`/breakpoint rules in `src/style.css`
 * (`--combat-hud-collapse-1` etc.) — weapon/assist collapse before
 * elapsed/kills, and hull/critical/pause never collapse.
 */
export const COMBAT_HUD_SEMANTIC_STATE_TABLE: readonly CombatHudSemanticStateEntry[] = [
  {
    id: 'hull',
    displayCondition: 'always while running',
    placement: 'bottom-left-status',
    collapsible: false,
    copy: '<current>/<max> Hull',
  },
  {
    id: 'critical',
    displayCondition: `hull ratio <= ${COMBAT_HUD_CRITICAL_HULL_RATIO} while running`,
    placement: 'bottom-left-status',
    collapsible: false,
    copy: 'Hull critical',
  },
  {
    id: 'kills',
    displayCondition: 'always while running',
    placement: 'bottom-left-status',
    collapsible: 2,
    copy: '<count> Kills',
  },
  {
    id: 'elapsed',
    displayCondition: 'always while running',
    placement: 'top-center-low-prominence',
    collapsible: 1,
    copy: '<seconds> s',
  },
  {
    id: 'weapon',
    displayCondition: 'always while running',
    placement: 'edge-control',
    collapsible: 3,
    copy: 'Ready | Recharging',
  },
  {
    id: 'assist',
    displayCondition: 'always while running (button disabled outside running)',
    placement: 'edge-control',
    collapsible: 3,
    copy: 'Assist allies / Assist ready. / Assist signal sent. / Allies covering you. / No target to assist. / No ally available. / Assist is available during sortie.',
  },
  {
    id: 'pause',
    displayCondition: 'always while running',
    placement: 'separate-pause-control',
    collapsible: false,
    copy: 'Pause',
  },
]

/**
 * View model for the combat HUD (AC1, AC4, AC6). `elapsedLabel` is derived
 * from `elapsedTicks * activeFixedDeltaMs` (never `/ 60`, wall-clock, or
 * rAF time) so the running timer is deterministic under E2E/VRT fixtures.
 */
export interface CombatHudViewModel {
  hullLabel: string
  isCritical: boolean
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
  // Issue #1375 body + PR #1925 review (P1-2): the Issue's own worked
  // example is "elapsedTicks: 900, fixedDeltaMs: 16 -> 14.4 s" (space
  // before the unit) — match that exactly rather than the no-space `14.4s`
  // this previously rendered.
  return `${elapsedSeconds.toFixed(1)} s`
}

/**
 * Derives whether the persistent status cluster should show the critical
 * warning (AC6: text/shape/icon-driven, never color-only). Guards
 * `maxHp <= 0` fail-closed to `false` (never critical for a malformed
 * state) rather than dividing by zero.
 */
export function isCombatHudHullCritical(hp: number, maxHp: number): boolean {
  if (!(maxHp > 0)) {
    return false
  }
  return hp / maxHp <= COMBAT_HUD_CRITICAL_HULL_RATIO
}

/**
 * Builds the combat HUD view model (AC1, AC2, AC4, AC6). Pure function of
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
    isCritical: isCombatHudHullCritical(state.player.hp, state.player.maxHp),
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
 * The combat HUD DOM fragment (AC1, AC2, AC3, AC4, AC5, AC6). Consumed by
 * `HudController.ts`'s `createHudController()`, which composes it alongside
 * the legacy result/debrief surface and owns phase routing / visibility.
 *
 * Placement/safe-zone (Issue #1958 Current Validated Scope): the root is a
 * transparent, `pointer-events: none` grid spanning the whole
 * `.battle-hud-layer` safe-zone box (`src/style.css` gives that box the
 * 16 CSS px inner containment margin, AC3). Its named grid areas place:
 * - `data-hud-zone="status"` (Hull/critical/Kills): persistent, bottom-left.
 * - `data-hud-zone="elapsed"`: low-prominence, top-center.
 * - `data-hud-zone="edge-control"` (Weapon/Assist): conditional, bottom-right,
 *   first to collapse under progressive disclosure (AC1 semantic table).
 * - `data-hud-zone="pause"`: separated from the status cluster, bottom-right,
 *   never collapses (AC5/AC6 keyboard/pointer affordance stays reachable).
 */
export const COMBAT_HUD_MARKUP = `
  <section class="combat-hud" data-combat-hud hidden inert>
    <p
      class="combat-hud__elapsed"
      data-hud-zone="elapsed"
      data-field="combat-hud-elapsed"
    ></p>
    <div class="combat-hud__status" data-hud-zone="status">
      <p class="eyebrow">Combat</p>
      <dl class="stat-grid">
        <div><dt>Hull</dt><dd data-field="combat-hud-hull"></dd></div>
        <div class="combat-hud__kills-row"><dt>Kills</dt><dd data-field="combat-hud-kills"></dd></div>
      </dl>
      <p class="combat-hud__critical" data-field="combat-hud-critical" role="status" aria-live="polite" hidden>
        <span aria-hidden="true">&#9888;</span> Hull critical
      </p>
    </div>
    <div class="combat-hud__edge" data-hud-zone="edge-control">
      <p class="combat-hud__weapon" data-field="combat-hud-weapon"></p>
      <button
        type="button"
        data-action="assist-player"
        data-battle-interactive="true"
        aria-label="Assist allies"
      >Assist allies</button>
      <p
        class="status-copy combat-hud__assist-status"
        data-field="combat-hud-assist-status"
        role="status"
        aria-live="polite"
        aria-atomic="true"
      ></p>
    </div>
    <button
      type="button"
      class="combat-hud__pause"
      data-hud-zone="pause"
      data-action="toggle-pause"
      data-battle-interactive="true"
      aria-pressed="false"
      title="Pause or resume simulation. Also toggled by Escape."
    >Pause</button>
  </section>
`
