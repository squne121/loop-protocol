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
  const canvas = battleStage?.querySelector<HTMLCanvasElement>(':scope > .battle-stage__canvas') ?? null
  const commandRail = root.querySelector<HTMLElement>('.command-rail')
  const uiLayer =
    battleStage?.querySelector<HTMLElement>(':scope > .battle-ui-layer[data-battle-ui-root]') ??
    null
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

export function configureBattleOverlayFoundation(elements: Pick<BattleOverlayElements, 'commandRail' | 'screenLayer'>): void {
  const appShell = elements.commandRail.closest<HTMLElement>('.app-shell')

  if (appShell) {
    appShell.setAttribute('data-battle-layout', 'overlay-hud')
  }

  elements.commandRail.replaceChildren()
  elements.commandRail.hidden = true
  elements.commandRail.setAttribute('aria-hidden', 'true')
  elements.commandRail.setAttribute('data-battle-placeholder', 'true')
  elements.screenLayer.hidden = true
  elements.screenLayer.setAttribute('inert', '')
  elements.screenLayer.setAttribute('aria-hidden', 'true')
}
