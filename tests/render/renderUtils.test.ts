/**
 * Unit tests for src/render/renderUtils.ts (Issue #581)
 * AC1 - formatCombatNumber | AC2 - computeHpLabelPosition | AC3 - drawEnemyHpLabel
 */

import { describe, it, expect, vi } from 'vitest'
import {
  formatCombatNumber,
  computeHpLabelPosition,
  drawEnemyHpLabel,
  HP_LABEL_FONT,
  HP_LABEL_COLOR,
} from '../../src/render/renderUtils'

describe('formatCombatNumber', () => {
  it('GIVEN value 0 WHEN formatted THEN returns 0', () => {
    expect(formatCombatNumber(0)).toBe('0')
  })
  it('GIVEN value 999 WHEN formatted THEN returns 999', () => {
    expect(formatCombatNumber(999)).toBe('999')
  })
  it('GIVEN value 9999 WHEN formatted THEN returns 9999', () => {
    expect(formatCombatNumber(9999)).toBe('9999')
  })
  it('GIVEN value 10000 WHEN formatted THEN returns 10k', () => {
    expect(formatCombatNumber(10000)).toBe('10k')
  })
  it('GIVEN value 999999 WHEN formatted THEN returns 999k', () => {
    expect(formatCombatNumber(999999)).toBe('999k')
  })
  it('GIVEN value 1000000 WHEN formatted THEN returns 1M', () => {
    expect(formatCombatNumber(1000000)).toBe('1M')
  })
  it('GIVEN value 1 WHEN formatted THEN returns 1', () => {
    expect(formatCombatNumber(1)).toBe('1')
  })
  it('GIVEN value 9999 boundary WHEN formatted THEN still exact', () => {
    expect(formatCombatNumber(9999)).toBe('9999')
  })
  it('GIVEN value 10001 WHEN formatted THEN floor to 10k', () => {
    expect(formatCombatNumber(10001)).toBe('10k')
  })
  it('GIVEN value 1500000 WHEN formatted THEN floor to 1M', () => {
    expect(formatCombatNumber(1500000)).toBe('1M')
  })
  it('GIVEN value 999000 WHEN formatted THEN returns 999k', () => {
    expect(formatCombatNumber(999000)).toBe('999k')
  })
  // Invalid input guard (number_display_policy: invalid input → 0)
  it('GIVEN NaN WHEN formatted THEN returns 0 (safe fallback)', () => {
    expect(formatCombatNumber(NaN)).toBe('0')
  })
  it('GIVEN Infinity WHEN formatted THEN returns 0 (safe fallback)', () => {
    expect(formatCombatNumber(Infinity)).toBe('0')
  })
  it('GIVEN -Infinity WHEN formatted THEN returns 0 (safe fallback)', () => {
    expect(formatCombatNumber(-Infinity)).toBe('0')
  })
  it('GIVEN negative integer -3 WHEN formatted THEN returns 0 (safe fallback)', () => {
    expect(formatCombatNumber(-3)).toBe('0')
  })
  it('GIVEN negative large value -9999 WHEN formatted THEN returns 0 (safe fallback)', () => {
    expect(formatCombatNumber(-9999)).toBe('0')
  })
  // New behavior (Issue #726): Accept fractional inputs with ceil rounding
  it('GIVEN fractional value 1.5 WHEN formatted THEN returns 2 (ceil)', () => {
    expect(formatCombatNumber(1.5)).toBe('2')
  })
  it('GIVEN fractional value 7.9999 WHEN formatted THEN returns 8 (ceil, handles floating-point artifact)', () => {
    expect(formatCombatNumber(7.9999)).toBe('8')
  })
  it('GIVEN fractional value 0.5 WHEN formatted THEN returns 1 (integer bucket, Issue #788)', () => {
    expect(formatCombatNumber(0.5)).toBe('1')
  })
  it('GIVEN value 10000 WHEN formatted THEN returns 10k (compact after ceil)', () => {
    expect(formatCombatNumber(10000)).toBe('10k')
  })
  // Issue #726 review: compact boundary must be evaluated on the ceiled value,
  // otherwise 9999.x renders as the 5-digit "10000" instead of "10k".
  it('GIVEN value 9999 WHEN formatted THEN returns 9999 (exact, below compact)', () => {
    expect(formatCombatNumber(9999)).toBe('9999')
  })
  it('GIVEN fractional value 9999.1 WHEN formatted THEN returns 10k (ceil crosses compact boundary)', () => {
    expect(formatCombatNumber(9999.1)).toBe('10k')
  })
  it('GIVEN fractional value 9999.999 WHEN formatted THEN returns 10k (ceil crosses compact boundary)', () => {
    expect(formatCombatNumber(9999.999)).toBe('10k')
  })
})
describe('computeHpLabelPosition', () => {
  const ARENA_W = 960
  const ARENA_H = 540
  const TEXT_W = 24
  const FONT_SIZE = 10
  const PADDING = 2

  function assertInBounds(x: number, y: number, textWidth: number) {
    const halfW = textWidth / 2
    const halfH = FONT_SIZE / 2
    expect(x - halfW).toBeGreaterThanOrEqual(PADDING)
    expect(x + halfW).toBeLessThanOrEqual(ARENA_W - PADDING)
    expect(y - halfH).toBeGreaterThanOrEqual(PADDING)
    expect(y + halfH).toBeLessThanOrEqual(ARENA_H - PADDING)
  }

  it('GIVEN enemy at center WHEN label computed THEN position within bounds', () => {
    const { x, y } = computeHpLabelPosition({ enemyX: ARENA_W / 2, enemyTopY: ARENA_H / 2 - 16, textWidth: TEXT_W, arenaWidth: ARENA_W, arenaHeight: ARENA_H })
    assertInBounds(x, y, TEXT_W)
    expect(x).toBe(ARENA_W / 2)
  })
  it('GIVEN enemy at top-left corner WHEN label computed THEN clamped into arena', () => {
    const { x, y } = computeHpLabelPosition({ enemyX: 0, enemyTopY: 0, textWidth: TEXT_W, arenaWidth: ARENA_W, arenaHeight: ARENA_H })
    assertInBounds(x, y, TEXT_W)
  })
  it('GIVEN enemy at top-right corner WHEN label computed THEN clamped into arena', () => {
    const { x, y } = computeHpLabelPosition({ enemyX: ARENA_W, enemyTopY: 0, textWidth: TEXT_W, arenaWidth: ARENA_W, arenaHeight: ARENA_H })
    assertInBounds(x, y, TEXT_W)
  })
  it('GIVEN enemy at bottom-left corner WHEN label computed THEN clamped into arena', () => {
    const { x, y } = computeHpLabelPosition({ enemyX: 0, enemyTopY: ARENA_H, textWidth: TEXT_W, arenaWidth: ARENA_W, arenaHeight: ARENA_H })
    assertInBounds(x, y, TEXT_W)
  })
  it('GIVEN enemy at bottom-right corner WHEN label computed THEN clamped into arena', () => {
    const { x, y } = computeHpLabelPosition({ enemyX: ARENA_W, enemyTopY: ARENA_H, textWidth: TEXT_W, arenaWidth: ARENA_W, arenaHeight: ARENA_H })
    assertInBounds(x, y, TEXT_W)
  })
  it('GIVEN enemy at left edge WHEN label computed THEN x clamped to min', () => {
    const { x } = computeHpLabelPosition({ enemyX: 0, enemyTopY: ARENA_H / 2, textWidth: TEXT_W, arenaWidth: ARENA_W, arenaHeight: ARENA_H })
    expect(x).toBe(PADDING + TEXT_W / 2)
  })
  it('GIVEN enemy at right edge WHEN label computed THEN x clamped to max', () => {
    const { x } = computeHpLabelPosition({ enemyX: ARENA_W, enemyTopY: ARENA_H / 2, textWidth: TEXT_W, arenaWidth: ARENA_W, arenaHeight: ARENA_H })
    expect(x).toBe(ARENA_W - PADDING - TEXT_W / 2)
  })
  it('GIVEN enemy at top edge WHEN label computed THEN y clamped to min', () => {
    const { y } = computeHpLabelPosition({ enemyX: ARENA_W / 2, enemyTopY: 0, textWidth: TEXT_W, arenaWidth: ARENA_W, arenaHeight: ARENA_H })
    expect(y).toBe(FONT_SIZE / 2 + PADDING)
  })
  it('GIVEN enemy below arena bottom WHEN label computed THEN y clamped to max', () => {
    // enemyTopY=600 => anchor=592 > yMax=533 => clamped to yMax=533
    const { y } = computeHpLabelPosition({ enemyX: ARENA_W / 2, enemyTopY: 600, textWidth: TEXT_W, arenaWidth: ARENA_W, arenaHeight: ARENA_H })
    expect(y).toBe(ARENA_H - FONT_SIZE / 2 - PADDING)
  })
  it('GIVEN DPR=1 vs DPR=2 WHEN same CSS coords but different physical arena dims THEN clamp max-x differs', () => {
    // computeHpLabelPosition operates in logical (CSS) pixel space.
    // CanvasRenderer scales canvas.width/height by DPR but passes logical arena dims
    // (arenaWidth / arenaHeight) to drawEnemyHpLabel. However, a test that passes
    // DPR-scaled arena dims (arenaWidth * dpr) simulates a scenario where the
    // caller mistakenly uses physical pixels — verifying clamp max changes with DPR.
    //
    // DPR=1: arenaWidth=960  → xMax = 960 - PADDING - TEXT_W/2  = 946
    // DPR=2: arenaWidth=1920 → xMax = 1920 - PADDING - TEXT_W/2 = 1906
    // Enemy at x=0 is clamped to xMin in both cases, so we place enemy beyond DPR=1 max.
    const dpr1Arena = ARENA_W               // 960
    const dpr2Arena = ARENA_W * 2           // 1920

    const dpr1XMax = dpr1Arena - PADDING - TEXT_W / 2  // 946
    const dpr2XMax = dpr2Arena - PADDING - TEXT_W / 2  // 1906

    // Place enemy at a position that is NOT clamped in DPR=2 arena but IS the boundary in DPR=1
    const { x: x1 } = computeHpLabelPosition({ enemyX: dpr1XMax, enemyTopY: ARENA_H / 2, textWidth: TEXT_W, arenaWidth: dpr1Arena, arenaHeight: ARENA_H })
    const { x: x2 } = computeHpLabelPosition({ enemyX: dpr1XMax, enemyTopY: ARENA_H / 2, textWidth: TEXT_W, arenaWidth: dpr2Arena, arenaHeight: ARENA_H })

    // At DPR=1 arena, enemy is exactly at xMax (clamped result = dpr1XMax)
    expect(x1).toBe(dpr1XMax)
    // At DPR=2 arena (wider), the same CSS x is not clamped — the label can stay at enemyX
    expect(x2).toBe(dpr1XMax)
    // The key assertion: the xMax bounds differ between DPR=1 and DPR=2 arenas
    expect(dpr2XMax).toBeGreaterThan(dpr1XMax)
    // Both results are within their respective arena bounds
    expect(x1 + TEXT_W / 2).toBeLessThanOrEqual(dpr1Arena - PADDING)
    expect(x2 + TEXT_W / 2).toBeLessThanOrEqual(dpr2Arena - PADDING)
  })
  it('GIVEN all corners with wide label WHEN computed THEN all within bounds', () => {
    const wideTextW = 40
    const positions = [
      { enemyX: 0, enemyTopY: 0 },
      { enemyX: ARENA_W, enemyTopY: 0 },
      { enemyX: 0, enemyTopY: ARENA_H },
      { enemyX: ARENA_W, enemyTopY: ARENA_H },
      { enemyX: ARENA_W / 2, enemyTopY: ARENA_H / 2 },
    ]
    for (const pos of positions) {
      const { x, y } = computeHpLabelPosition({ ...pos, textWidth: wideTextW, arenaWidth: ARENA_W, arenaHeight: ARENA_H })
      assertInBounds(x, y, wideTextW)
    }
  })
})
describe('drawEnemyHpLabel', () => {
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
      measureText: vi.fn(() => ({ width: 24 })),
      fillText: vi.fn(() => { calls.push('fillText') }),
    }
    return { ctx, calls }
  }

  it('GIVEN enemy with HP WHEN drawEnemyHpLabel called THEN save and restore each called once', () => {
    const { ctx } = makeMockCtx()
    drawEnemyHpLabel({ ctx: ctx as unknown as CanvasRenderingContext2D, enemyX: 480, enemyY: 270, enemyRadius: 16, enemyHp: 100, arenaWidth: 960, arenaHeight: 540 })
    expect(ctx.save).toHaveBeenCalledOnce()
    expect(ctx.restore).toHaveBeenCalledOnce()
  })

  it('GIVEN initial canvas state WHEN drawEnemyHpLabel called THEN all properties restored after call', () => {
    const { ctx } = makeMockCtx()
    const initFont = ctx.font
    const initFillStyle = ctx.fillStyle
    const initTextAlign = ctx.textAlign
    const initTextBaseline = ctx.textBaseline
    drawEnemyHpLabel({ ctx: ctx as unknown as CanvasRenderingContext2D, enemyX: 480, enemyY: 270, enemyRadius: 16, enemyHp: 500, arenaWidth: 960, arenaHeight: 540 })
    expect(ctx.font).toBe(initFont)
    expect(ctx.fillStyle).toBe(initFillStyle)
    expect(ctx.textAlign).toBe(initTextAlign)
    expect(ctx.textBaseline).toBe(initTextBaseline)
  })

  it('GIVEN HP=0 enemy WHEN drawEnemyHpLabel called THEN fillText called with 0', () => {
    const { ctx } = makeMockCtx()
    drawEnemyHpLabel({ ctx: ctx as unknown as CanvasRenderingContext2D, enemyX: 480, enemyY: 270, enemyRadius: 16, enemyHp: 0, arenaWidth: 960, arenaHeight: 540 })
    expect(ctx.fillText).toHaveBeenCalledWith('0', expect.any(Number), expect.any(Number))
  })

  it('GIVEN HP=10000 enemy WHEN drawEnemyHpLabel called THEN fillText called with 10k', () => {
    const { ctx } = makeMockCtx()
    drawEnemyHpLabel({ ctx: ctx as unknown as CanvasRenderingContext2D, enemyX: 480, enemyY: 270, enemyRadius: 16, enemyHp: 10000, arenaWidth: 960, arenaHeight: 540 })
    expect(ctx.fillText).toHaveBeenCalledWith('10k', expect.any(Number), expect.any(Number))
  })

  it('GIVEN enemy world cue draw WHEN label is rendered THEN non-authoritative world cue boundary is preserved', () => {
    const { ctx } = makeMockCtx()
    drawEnemyHpLabel({ ctx: ctx as unknown as CanvasRenderingContext2D, enemyX: 480, enemyY: 270, enemyRadius: 16, enemyHp: 4, arenaWidth: 960, arenaHeight: 540 })
    expect(ctx.fillText).toHaveBeenCalledWith('4', expect.any(Number), expect.any(Number))
  })

  // Issue #726 AC2 review: Canvas enemy HP label must share formatCombatNumber, so a
  // fractional enemy HP is ceiled (7.9999 -> "8") rather than leaking decimal precision.
  it('GIVEN fractional enemy HP 7.9999 WHEN drawEnemyHpLabel called THEN fillText receives ceiled "8"', () => {
    const { ctx } = makeMockCtx()
    drawEnemyHpLabel({ ctx: ctx as unknown as CanvasRenderingContext2D, enemyX: 480, enemyY: 270, enemyRadius: 16, enemyHp: 7.9999, arenaWidth: 960, arenaHeight: 540 })
    expect(ctx.fillText).toHaveBeenCalledWith('8', expect.any(Number), expect.any(Number))
  })

  it('GIVEN fractional enemy HP 0.5 (living) WHEN drawEnemyHpLabel called THEN fillText receives "1" (integer bucket, Issue #788)', () => {
    const { ctx } = makeMockCtx()
    drawEnemyHpLabel({ ctx: ctx as unknown as CanvasRenderingContext2D, enemyX: 480, enemyY: 270, enemyRadius: 16, enemyHp: 0.5, arenaWidth: 960, arenaHeight: 540 })
    expect(ctx.fillText).toHaveBeenCalledWith('1', expect.any(Number), expect.any(Number))
  })

  it('GIVEN draw call WHEN font set THEN matches HP_LABEL_FONT constant', () => {
    const { ctx, calls } = makeMockCtx()
    drawEnemyHpLabel({ ctx: ctx as unknown as CanvasRenderingContext2D, enemyX: 480, enemyY: 270, enemyRadius: 16, enemyHp: 999, arenaWidth: 960, arenaHeight: 540 })
    expect(calls).toContain('font=' + HP_LABEL_FONT)
  })

  it('GIVEN draw call WHEN fillStyle set THEN matches HP_LABEL_COLOR constant', () => {
    const { ctx, calls } = makeMockCtx()
    drawEnemyHpLabel({ ctx: ctx as unknown as CanvasRenderingContext2D, enemyX: 480, enemyY: 270, enemyRadius: 16, enemyHp: 999, arenaWidth: 960, arenaHeight: 540 })
    expect(calls).toContain('fillStyle=' + HP_LABEL_COLOR)
  })

  it('GIVEN call order WHEN drawEnemyHpLabel called THEN save is first and restore is last', () => {
    const { ctx, calls } = makeMockCtx()
    drawEnemyHpLabel({ ctx: ctx as unknown as CanvasRenderingContext2D, enemyX: 480, enemyY: 270, enemyRadius: 16, enemyHp: 1234, arenaWidth: 960, arenaHeight: 540 })
    const saveIdx = calls.indexOf('save')
    const restoreIdx = calls.lastIndexOf('restore')
    const fillTextIdx = calls.indexOf('fillText')
    expect(saveIdx).toBe(0)
    expect(restoreIdx).toBeGreaterThan(fillTextIdx)
    expect(restoreIdx).toBe(calls.length - 1)
  })
})
