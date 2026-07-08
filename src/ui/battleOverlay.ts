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
  const canvas = root.querySelector<HTMLCanvasElement>('.battle-stage__canvas')
  const commandRail = root.querySelector<HTMLElement>('.command-rail')
  const uiLayer = root.querySelector<HTMLElement>('.battle-ui-layer')
  const hudLayer = root.querySelector<HTMLElement>('.battle-hud-layer')
  const screenLayer = root.querySelector<HTMLElement>('.battle-screen-layer')

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
  elements.commandRail.replaceChildren()
  elements.commandRail.setAttribute('aria-hidden', 'true')
  elements.commandRail.setAttribute('data-battle-placeholder', 'true')
  elements.screenLayer.hidden = true
  elements.screenLayer.setAttribute('inert', '')
}
