/**
 * @vitest-environment jsdom
 */

import { describe, expect, it } from 'vitest'

import {
  configureBattleOverlayFoundation,
  resolveBattleOverlayElements,
} from '../src/ui/battleOverlay'

function renderShell() {
  document.body.innerHTML = `
    <div class="app-shell">
      <section class="battle-stage">
        <div class="battle-stage__header"></div>
        <canvas class="battle-stage__canvas"></canvas>
        <div class="battle-ui-layer" data-battle-ui-root>
          <div class="battle-hud-layer" data-battle-layer="hud"></div>
          <div class="battle-screen-layer" data-battle-layer="screen"></div>
        </div>
      </section>
      <aside class="command-rail" aria-label="Command rail">
        <button type="button" data-action="legacy">Legacy</button>
      </aside>
    </div>
  `
}

describe('battleOverlay', () => {
  it('GIVEN a battle-stage shell WHEN resolved THEN overlay layers and placeholder rail are returned', () => {
    renderShell()

    const overlay = resolveBattleOverlayElements(document)

    expect(overlay).not.toBeNull()
    expect(overlay?.uiLayer.dataset.battleUiRoot).toBe('')
    expect(overlay?.hudLayer.dataset.battleLayer).toBe('hud')
    expect(overlay?.screenLayer.dataset.battleLayer).toBe('screen')
  })

  it('GIVEN legacy rail content WHEN foundation is configured THEN command rail is emptied and screen layer becomes inactive', () => {
    renderShell()

    const overlay = resolveBattleOverlayElements(document)
    expect(overlay).not.toBeNull()

    configureBattleOverlayFoundation(overlay!)

    expect(overlay?.commandRail.children).toHaveLength(0)
    expect(overlay?.commandRail.hidden).toBe(true)
    expect(overlay?.commandRail.getAttribute('data-battle-placeholder')).toBe('true')
    expect(document.querySelector('.app-shell')?.getAttribute('data-battle-layout')).toBe('overlay-hud')
    expect(overlay?.screenLayer.hidden).toBe(true)
    expect(overlay?.screenLayer.hasAttribute('inert')).toBe(true)
    expect(overlay?.screenLayer.getAttribute('aria-hidden')).toBe('true')
  })

  it('GIVEN a stray battle-hud-layer outside battle-ui-layer WHEN resolved THEN resolver fails closed', () => {
    renderShell()
    document.body.insertAdjacentHTML(
      'beforeend',
      '<div class="battle-hud-layer" data-battle-layer="hud"></div>',
    )
    document.querySelector('.battle-ui-layer .battle-hud-layer')?.remove()

    expect(resolveBattleOverlayElements(document)).toBeNull()
  })
})
