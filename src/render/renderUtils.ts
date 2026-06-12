/**
 * renderUtils.ts — combat numeric display helpers (Issue #581)
 *
 * Exports:
 *   - formatCombatNumber: deterministic compact notation (number_display_policy SSOT)
 *   - drawEnemyHpLabel: measure -> clamp -> draw with save()/restore() isolation
 *
 * Design: format -> measureText -> bounds clamp -> draw
 * maxWidth is NOT used (browser-dependent compression behaviour; no readability guarantee).
 */

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Font used for enemy HP labels. */
export const HP_LABEL_FONT = '10px sans-serif'

/** Fill colour for enemy HP labels. */
export const HP_LABEL_COLOR = '#ffffff'

/** Minimum padding (logical px) between the label bounding box and arena edge. */
export const HP_LABEL_PADDING = 2

// ---------------------------------------------------------------------------
// formatCombatNumber (number_display_policy SSOT)
// ---------------------------------------------------------------------------

/**
 * Format an HP value for display (DOM HUD and Canvas enemy HP labels).
 *
 * Policy (SSOT: docs/product/features/combat-core.md#combat-numeric-display-policy):
 *   NaN / Infinity / negative             -> "0"
 *   value === 0                           -> "0"
 *   0 < value < 1                         -> Math.ceil(value) = "1" (integer bucket; Issue #788)
 *   value >= 1                            -> displayValue = Math.ceil(value), then:
 *       displayValue < 10000              -> String(displayValue)        ("9999", "8")
 *       displayValue < 1000000            -> floor(displayValue/1e3)+"k" ("10k", "999k")
 *       otherwise                         -> floor(displayValue/1e6)+"M" ("1M")
 *
 * The compact boundary is evaluated on the *ceiled* value, so 9999.1 -> ceil 10000
 * -> "10k" (not the 5-digit "10000"). This is a human-readable display bucket, not
 * an exact HP value: do not reuse for damage log / balance / persistence / score.
 *
 * Input domain: finite non-negative numbers (integers or floats).
 * Safe fallback is used rather than throwing — this is a rendering-layer function.
 * Output is locale-independent.
 */
export function formatCombatNumber(value: number): string {
  // Invalid input guard: NaN, Infinity, negative
  if (!Number.isFinite(value) || value < 0) {
    return '0'
  }
  // Exact zero
  if (value === 0) {
    return '0'
  }
  // 0 < value: apply Math.ceil to produce an integer bucket (Issue #788 policy).
  // For 0 < value < 1: Math.ceil returns 1, so living units with sub-1 HP appear as "1".
  // This replaces the previous "<1" output. Player-facing normal UI must show integers only.
  // For value >= 1: ceil FIRST, then evaluate the compact boundary on the ceiled value.
  // Branching on the raw value would render 9999.1 as the 5-digit "10000" because
  // 9999.1 < 10000 is true before rounding; ceiling first makes it 10000 -> "10k",
  // matching number_display_policy (#581: compact_from 10000).
  const displayValue = Math.ceil(value)
  if (displayValue < 10_000) {
    return String(displayValue)
  }
  if (displayValue < 1_000_000) {
    return String(Math.floor(displayValue / 1_000)) + 'k'
  }
  return String(Math.floor(displayValue / 1_000_000)) + 'M'
}

// ---------------------------------------------------------------------------
// HP label layout helpers
// ---------------------------------------------------------------------------

/**
 * Parameters for computing a clamped HP label position.
 */
export interface HpLabelParams {
  /** Enemy center X in logical (CSS) pixels. */
  enemyX: number
  /** Enemy top Y in logical (CSS) pixels (= enemy.y - enemy.radius). */
  enemyTopY: number
  /** Measured text width from ctx.measureText (logical pixels). */
  textWidth: number
  /** Arena width in logical pixels. */
  arenaWidth: number
  /** Arena height in logical pixels. */
  arenaHeight: number
  /**
   * Font size in logical pixels (used for vertical half-extent of bounding box).
   * Default: 10 (matches HP_LABEL_FONT "10px sans-serif").
   */
  fontSize?: number
  /**
   * Padding between label bounding box and arena edge (logical pixels).
   * Default: HP_LABEL_PADDING (2).
   */
  padding?: number
}

/**
 * Compute the clamped (x, y) draw position so the label bounding box stays
 * entirely within arena bounds.
 *
 * The (x, y) is the canvas fillText anchor when:
 *   textAlign = "center", textBaseline = "middle"
 *
 * Clamp logic (logical px):
 *   x: clamp to [padding + textWidth/2, arenaWidth - padding - textWidth/2]
 *   y: clamp to [fontSize/2 + padding, arenaHeight - fontSize/2 - padding]
 */
export function computeHpLabelPosition(params: HpLabelParams): { x: number; y: number } {
  const {
    enemyX,
    enemyTopY,
    textWidth,
    arenaWidth,
    arenaHeight,
    fontSize = 10,
    padding = HP_LABEL_PADDING,
  } = params

  // Anchor: enemy center x, enemy top y - 8px (above the enemy circle)
  const anchorX = enemyX
  const anchorY = enemyTopY - 8

  const halfW = textWidth / 2
  const halfH = fontSize / 2

  const xMin = padding + halfW
  const xMax = arenaWidth - padding - halfW
  const yMin = halfH + padding
  const yMax = arenaHeight - halfH - padding

  const x = Math.max(xMin, Math.min(xMax, anchorX))
  const y = Math.max(yMin, Math.min(yMax, anchorY))

  return { x, y }
}

// ---------------------------------------------------------------------------
// drawEnemyHpLabel
// ---------------------------------------------------------------------------

/**
 * Parameters for drawEnemyHpLabel.
 */
export interface DrawEnemyHpLabelParams {
  /** 2D rendering context. */
  ctx: CanvasRenderingContext2D
  /** Enemy center X (logical px). */
  enemyX: number
  /** Enemy center Y (logical px). */
  enemyY: number
  /** Enemy radius (logical px) -- used to compute top Y. */
  enemyRadius: number
  /** Enemy current HP (integer >= 0). */
  enemyHp: number
  /** Arena width (logical px). */
  arenaWidth: number
  /** Arena height (logical px). */
  arenaHeight: number
}

/**
 * Draw the HP label for one enemy.
 *
 * Uses save()/restore() to isolate all canvas state changes:
 *   font, fillStyle, textAlign, textBaseline.
 *
 * Render pipeline: format -> measureText -> bounds clamp -> fillText
 */
export function drawEnemyHpLabel(params: DrawEnemyHpLabelParams): void {
  const { ctx, enemyX, enemyY, enemyRadius, enemyHp, arenaWidth, arenaHeight } = params

  const label = formatCombatNumber(enemyHp)

  ctx.save()
  try {
    ctx.font = HP_LABEL_FONT
    ctx.fillStyle = HP_LABEL_COLOR
    ctx.textAlign = 'center'
    ctx.textBaseline = 'middle'

    const measured = ctx.measureText(label)
    const textWidth = measured.width

    const enemyTopY = enemyY - enemyRadius

    const { x, y } = computeHpLabelPosition({
      enemyX,
      enemyTopY,
      textWidth,
      arenaWidth,
      arenaHeight,
    })

    ctx.fillText(label, x, y)
  } finally {
    ctx.restore()
  }
}
