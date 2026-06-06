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
 * Format an integer HP value for canvas display.
 *
 * Policy (SSOT: docs/product/features/combat-core.md#combat-numeric-display-policy):
 *   0 - 9999  -> exact string ("0", "999", "9999")
 *   10000+    -> compact with floor rounding, lowercase unit
 *     10000 -> "10k", 999999 -> "999k", 1000000 -> "1M"
 *
 * Input domain: integer >= 0.
 * Invalid inputs (NaN, Infinity, negative, non-integer) are treated as 0.
 * Safe fallback is used rather than throwing — this is a rendering-layer function
 * (docs/product/features/combat-core.md number_display_policy: invalid input → 0).
 * Output is locale-independent.
 */
export function formatCombatNumber(value: number): string {
  if (!Number.isFinite(value) || !Number.isInteger(value) || value < 0) {
    return '0'
  }
  if (value < 10_000) {
    return String(Math.floor(value))
  }
  if (value < 1_000_000) {
    return String(Math.floor(value / 1_000)) + 'k'
  }
  return String(Math.floor(value / 1_000_000)) + 'M'
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
