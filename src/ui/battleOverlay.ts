export interface BattleOverlayElements {
  battleStage: HTMLElement
  canvas: HTMLCanvasElement
  commandRail: HTMLElement
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
  const commandRail = root.querySelector<HTMLElement>('.command-rail')
  const uiLayer =
    viewport?.querySelector<HTMLElement>(':scope > .battle-ui-layer[data-battle-ui-root]') ?? null
  const hudLayer =
    uiLayer?.querySelector<HTMLElement>(':scope > .battle-hud-layer[data-battle-layer="hud"]') ??
    null
  const screenLayer =
    uiLayer?.querySelector<HTMLElement>(
      ':scope > .battle-screen-layer[data-battle-layer="screen"]',
    ) ?? null

  if (!battleStage || !canvas || !commandRail || !uiLayer || !hudLayer || !screenLayer) {
    return null
  }

  return {
    battleStage,
    canvas,
    commandRail,
    uiLayer,
    hudLayer,
    screenLayer,
  }
}

export function syncBattleOverlayPlaceholderRail(
  elements: Pick<BattleOverlayElements, 'commandRail'>,
): void {
  const appShell = elements.commandRail.closest<HTMLElement>('.app-shell')

  if (appShell) {
    appShell.setAttribute('data-battle-layout', 'overlay-hud')
  }

  elements.commandRail.hidden = true
  elements.commandRail.setAttribute('aria-hidden', 'true')
  elements.commandRail.setAttribute('data-battle-placeholder', 'true')
}

export function configureBattleOverlayFoundation(
  elements: Pick<BattleOverlayElements, 'commandRail' | 'screenLayer'>,
): void {
  elements.commandRail.replaceChildren()
  syncBattleOverlayPlaceholderRail(elements)
  elements.screenLayer.hidden = true
  elements.screenLayer.setAttribute('inert', '')
  elements.screenLayer.setAttribute('aria-hidden', 'true')
}
