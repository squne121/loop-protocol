/**
 * @vitest-environment jsdom
 */

/**
 * Regression gate for player-facing HP/HULL display policy (Issue #788)
 *
 * Policy: normal UI (DOM HUD + Canvas enemy HP label) must never expose
 * "<1" or fractional strings for 0 < value < 1.
 * Instead, Math.ceil is applied so the minimum displayed integer is "1".
 *
 * AC7: formatCombatNumber(0.001), formatCombatNumber(0.5), formatCombatNumber(0.9999) -> "1"
 * AC8: HUD Hull shows "1/<max>" when player.hp = 0.5
 * AC9: Enemy HP label shows "1" when enemyHp = 0.5
 * AC11: Narrow fix — no other display policies are affected
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { formatCombatNumber, drawEnemyHpLabel } from '../src/render/renderUtils'
import { createHudController } from '../src/ui/HudController'
import type { GameState, LoopPhase, SortieResult } from '../src/state'

// ---------------------------------------------------------------------------
// Helper: minimal GameState factory
// ---------------------------------------------------------------------------

const TERMINAL_SORTIE_RESULT: SortieResult = {
  outcome: 'victory',
  endReason: 'all_enemies_defeated',
  durationMs: 30_000,
  kills: 4,
  shotsFired: 18,
  playerHpRemaining: 6,
}

function createState(overrides: { hp?: number; maxHp?: number; loopPhase?: LoopPhase } = {}): GameState {
  const loopPhase = overrides.loopPhase ?? 'preparation'
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
      hp: overrides.hp ?? 8,
      maxHp: overrides.maxHp ?? 8,
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
      claimedApplicationIds: Object.create(null) as Record<string, true>,
    },
    telemetry: {
      status: 'Combat systems green',
      lastCommandSummary: 'test',
    },
    sortie: {
      status: isDebrief ? 'completed' : 'idle',
      elapsedTicks: 0,
      targetTicks: 1800,
      result: isDebrief ? TERMINAL_SORTIE_RESULT : null,
    },
  }
}

function makeMockCtx() {
  const calls: string[] = []
  let _font = 'default-font'
  let _fillStyle: string | CanvasGradient | CanvasPattern = '#000000'
  let _textAlign: CanvasTextAlign = 'left'
  let _textBaseline: CanvasTextBaseline = 'alphabetic'
  const stack: Array<{ font: string; fillStyle: string | CanvasGradient | CanvasPattern; textAlign: CanvasTextAlign; textBaseline: CanvasTextBaseline }> = []
  const ctx = {
    get font() { return _font },
    set font(v: string) { calls.push('font=' + v); _font = v },
    get fillStyle() { return _fillStyle },
    set fillStyle(v: string | CanvasGradient | CanvasPattern) { calls.push('fillStyle=' + String(v)); _fillStyle = v },
    get textAlign() { return _textAlign },
    set textAlign(v: CanvasTextAlign) { calls.push('textAlign=' + v); _textAlign = v },
    get textBaseline() { return _textBaseline },
    set textBaseline(v: CanvasTextBaseline) { calls.push('textBaseline=' + v); _textBaseline = v },
    save: vi.fn(() => { calls.push('save'); stack.push({ font: _font, fillStyle: _fillStyle, textAlign: _textAlign, textBaseline: _textBaseline }) }),
    restore: vi.fn(() => { calls.push('restore'); const prev = stack.pop(); if (prev) { _font = prev.font; _fillStyle = prev.fillStyle; _textAlign = prev.textAlign; _textBaseline = prev.textBaseline } }),
    measureText: vi.fn(() => ({ width: 8 })),
    fillText: vi.fn(() => { calls.push('fillText') }),
  }
  return { ctx, calls }
}

// ---------------------------------------------------------------------------
// AC7: formatCombatNumber — sub-1 values must return "1" (integer bucket)
// ---------------------------------------------------------------------------

describe('player-facing HP/HULL policy — AC7: formatCombatNumber integer bucket', () => {
  it('GIVEN value 0.001 (near-zero living unit) WHEN formatted THEN returns "1" not "<1"', () => {
    expect(formatCombatNumber(0.001)).toBe('1')
  })

  it('GIVEN value 0.5 (half-HP living unit) WHEN formatted THEN returns "1" not "<1"', () => {
    expect(formatCombatNumber(0.5)).toBe('1')
  })

  it('GIVEN value 0.9999 (near-integer living unit) WHEN formatted THEN returns "1" not "<1"', () => {
    expect(formatCombatNumber(0.9999)).toBe('1')
  })

  it('GIVEN value 0 (zero) WHEN formatted THEN still returns "0" (defeat boundary unchanged)', () => {
    expect(formatCombatNumber(0)).toBe('0')
  })

  it('GIVEN value 1 (exact integer) WHEN formatted THEN still returns "1"', () => {
    expect(formatCombatNumber(1)).toBe('1')
  })

  it('GIVEN value 1.5 (above 1 fractional) WHEN formatted THEN returns "2" (ceil applied)', () => {
    expect(formatCombatNumber(1.5)).toBe('2')
  })

  it('GIVEN NaN WHEN formatted THEN returns "0" (safe fallback unchanged)', () => {
    expect(formatCombatNumber(NaN)).toBe('0')
  })

  it('GIVEN negative value WHEN formatted THEN returns "0" (safe fallback unchanged)', () => {
    expect(formatCombatNumber(-1)).toBe('0')
  })
})

// ---------------------------------------------------------------------------
// AC8: HUD Hull display — player.hp = 0.5 must show "1/<max>"
// ---------------------------------------------------------------------------

describe('player-facing HP/HULL policy — AC8: HUD Hull display with fractional hp', () => {
  let container: HTMLElement

  beforeEach(() => {
    container = document.createElement('div')
    createHudController(container, {
      onStartSortie: vi.fn(),
      onClaimReward: vi.fn(),
      onNextSortie: vi.fn(),
      onQuickSave: vi.fn(),
      onQuickLoad: vi.fn(),
      onReset: vi.fn(),
      canQuickLoad: vi.fn(() => true),
    })
  })

  it('GIVEN player.hp = 0.5 maxHp = 8 WHEN HUD rendered THEN Hull shows "1/8" not "<1/8"', () => {
    const hud = createHudController(container, {
      onStartSortie: vi.fn(),
      onClaimReward: vi.fn(),
      onNextSortie: vi.fn(),
      onQuickSave: vi.fn(),
      onQuickLoad: vi.fn(),
      onReset: vi.fn(),
      canQuickLoad: vi.fn(() => true),
    })
    hud.render(createState({ hp: 0.5, maxHp: 8 }))
    const hpField = container.querySelector('[data-field="hp"]')
    expect(hpField?.textContent).toBe('1/8')
  })

  it('GIVEN player.hp = 0.001 maxHp = 10 WHEN HUD rendered THEN Hull shows "1/10"', () => {
    const hud = createHudController(container, {
      onStartSortie: vi.fn(),
      onClaimReward: vi.fn(),
      onNextSortie: vi.fn(),
      onQuickSave: vi.fn(),
      onQuickLoad: vi.fn(),
      onReset: vi.fn(),
      canQuickLoad: vi.fn(() => true),
    })
    hud.render(createState({ hp: 0.001, maxHp: 10 }))
    const hpField = container.querySelector('[data-field="hp"]')
    expect(hpField?.textContent).toBe('1/10')
  })

  it('GIVEN player.hp = 0 (defeated) maxHp = 8 WHEN HUD rendered THEN Hull shows "0/8"', () => {
    const hud = createHudController(container, {
      onStartSortie: vi.fn(),
      onClaimReward: vi.fn(),
      onNextSortie: vi.fn(),
      onQuickSave: vi.fn(),
      onQuickLoad: vi.fn(),
      onReset: vi.fn(),
      canQuickLoad: vi.fn(() => true),
    })
    hud.render(createState({ hp: 0, maxHp: 8 }))
    const hpField = container.querySelector('[data-field="hp"]')
    expect(hpField?.textContent).toBe('0/8')
  })
})

// ---------------------------------------------------------------------------
// AC9: Canvas enemy HP label — enemyHp = 0.5 must show "1"
// ---------------------------------------------------------------------------

describe('player-facing HP/HULL policy — AC9: Canvas enemy HP label with fractional hp', () => {
  it('GIVEN enemyHp = 0.5 (living, sub-1) WHEN drawEnemyHpLabel called THEN fillText receives "1" not "<1"', () => {
    const { ctx } = makeMockCtx()
    drawEnemyHpLabel({
      ctx: ctx as unknown as CanvasRenderingContext2D,
      enemyX: 480,
      enemyY: 270,
      enemyRadius: 16,
      enemyHp: 0.5,
      arenaWidth: 960,
      arenaHeight: 540,
    })
    expect(ctx.fillText).toHaveBeenCalledWith('1', expect.any(Number), expect.any(Number))
  })

  it('GIVEN enemyHp = 0.001 WHEN drawEnemyHpLabel called THEN fillText receives "1"', () => {
    const { ctx } = makeMockCtx()
    drawEnemyHpLabel({
      ctx: ctx as unknown as CanvasRenderingContext2D,
      enemyX: 480,
      enemyY: 270,
      enemyRadius: 16,
      enemyHp: 0.001,
      arenaWidth: 960,
      arenaHeight: 540,
    })
    expect(ctx.fillText).toHaveBeenCalledWith('1', expect.any(Number), expect.any(Number))
  })

  it('GIVEN enemyHp = 0 (defeated) WHEN drawEnemyHpLabel called THEN fillText receives "0" (defeat boundary unchanged)', () => {
    const { ctx } = makeMockCtx()
    drawEnemyHpLabel({
      ctx: ctx as unknown as CanvasRenderingContext2D,
      enemyX: 480,
      enemyY: 270,
      enemyRadius: 16,
      enemyHp: 0,
      arenaWidth: 960,
      arenaHeight: 540,
    })
    expect(ctx.fillText).toHaveBeenCalledWith('0', expect.any(Number), expect.any(Number))
  })
})

// ---------------------------------------------------------------------------
// AC11: No-regression — display policy for value >= 1 and value === 0 is unchanged
// ---------------------------------------------------------------------------

describe('player-facing HP/HULL policy — AC11: narrow fix regression guard', () => {
  it('GIVEN integer values >= 1 WHEN formatted THEN exact string unchanged', () => {
    expect(formatCombatNumber(1)).toBe('1')
    expect(formatCombatNumber(99)).toBe('99')
    expect(formatCombatNumber(9999)).toBe('9999')
  })

  it('GIVEN compact boundary 10000 WHEN formatted THEN "10k"', () => {
    expect(formatCombatNumber(10000)).toBe('10k')
  })

  it('GIVEN value 999999 WHEN formatted THEN "999k"', () => {
    expect(formatCombatNumber(999999)).toBe('999k')
  })

  it('GIVEN value 1000000 WHEN formatted THEN "1M"', () => {
    expect(formatCombatNumber(1000000)).toBe('1M')
  })

  it('GIVEN value 0 WHEN formatted THEN "0" (defeat indicator unchanged)', () => {
    expect(formatCombatNumber(0)).toBe('0')
  })

  it('GIVEN fractional value 9999.1 WHEN formatted THEN "10k" (compact boundary still ceil-first)', () => {
    expect(formatCombatNumber(9999.1)).toBe('10k')
  })
})
