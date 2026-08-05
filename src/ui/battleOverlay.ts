export interface BattleOverlayElements {
  battleStage: HTMLElement
  canvas: HTMLCanvasElement
  uiLayer: HTMLElement
  hudLayer: HTMLElement
  screenLayer: HTMLElement
}

export function resolveBattleOverlayElements(root: ParentNode): BattleOverlayElements | null {
  const battleStage = root.querySelector<HTMLElement>('.battle-stage')
  // Issue #1375 PR #1925 review (P0-1): canvas + overlay layers live inside
  // `.battle-stage__viewport` now, not directly under `.battle-stage` (which
  // also contains `.battle-stage__header`) — see `src/main.ts` app shell
  // markup comment for why the containing block was split.
  const viewport = battleStage?.querySelector<HTMLElement>(':scope > .battle-stage__viewport') ?? null
  const canvas = viewport?.querySelector<HTMLCanvasElement>(':scope > .battle-stage__canvas') ?? null
  const uiLayer =
    viewport?.querySelector<HTMLElement>(':scope > .battle-ui-layer[data-battle-ui-root]') ?? null
  const hudLayer =
    uiLayer?.querySelector<HTMLElement>(':scope > .battle-hud-layer[data-battle-layer="hud"]') ??
    null
  const screenLayer =
    uiLayer?.querySelector<HTMLElement>(
      ':scope > .battle-screen-layer[data-battle-layer="screen"]',
    ) ?? null

  if (!battleStage || !canvas || !uiLayer || !hudLayer || !screenLayer) {
    return null
  }

  return {
    battleStage,
    canvas,
    uiLayer,
    hudLayer,
    screenLayer,
  }
}

/**
 * Configures the battle overlay's initial foundation state (Issue #1377: the
 * legacy `.command-rail` placeholder and its sync helper were removed —
 * `.battle-stage` overlay layers are the sole normal-play surface now). Only
 * the result/pause screen layer needs an initial inert/hidden state; ordinary
 * gameplay reveals it via `src/ui/phaseScreens.ts`'s controller.
 */
export function configureBattleOverlayFoundation(
  elements: Pick<BattleOverlayElements, 'screenLayer'>,
): void {
  elements.screenLayer.hidden = true
  elements.screenLayer.setAttribute('inert', '')
  elements.screenLayer.setAttribute('aria-hidden', 'true')
}
