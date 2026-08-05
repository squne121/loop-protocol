/**
 * @vitest-environment jsdom
 */

import { describe, expect, it } from 'vitest'

import { configureBattleOverlayFoundation, resolveBattleOverlayElements } from '../src/ui/battleOverlay'

function renderShell() {
  document.body.innerHTML = `
    <div class="app-shell">
      <section class="battle-stage">
        <div class="battle-stage__header"></div>
        <div class="battle-stage__viewport">
          <canvas class="battle-stage__canvas"></canvas>
          <div class="battle-ui-layer" data-battle-ui-root>
            <div class="battle-hud-layer" data-battle-layer="hud"></div>
            <div class="battle-screen-layer" data-battle-layer="screen"></div>
          </div>
        </div>
      </section>
    </div>
  `
}

describe('battleOverlay', () => {
  it('GIVEN a battle-stage shell with no legacy command rail WHEN resolved THEN overlay layers are returned (#1377)', () => {
    renderShell()

    const overlay = resolveBattleOverlayElements(document)

    expect(overlay).not.toBeNull()
    expect(overlay?.uiLayer.dataset.battleUiRoot).toBe('')
    expect(overlay?.hudLayer.dataset.battleLayer).toBe('hud')
    expect(overlay?.screenLayer.dataset.battleLayer).toBe('screen')
  })

  it('GIVEN the overlay shell WHEN foundation is configured THEN the screen layer becomes inactive and no command-rail markup is required (#1377)', () => {
    renderShell()

    const overlay = resolveBattleOverlayElements(document)
    expect(overlay).not.toBeNull()

    configureBattleOverlayFoundation(overlay!)

    expect(overlay?.screenLayer.hidden).toBe(true)
    expect(overlay?.screenLayer.hasAttribute('inert')).toBe(true)
    expect(overlay?.screenLayer.getAttribute('aria-hidden')).toBe('true')
    expect(document.querySelector('aside.command-rail')).toBeNull()
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

  it('GIVEN a battle-stage with no canvas or overlay layers WHEN resolved THEN resolver fails closed (#1377)', () => {
    document.body.innerHTML = `
      <div class="app-shell">
        <section class="battle-stage"></section>
      </div>
    `

    expect(resolveBattleOverlayElements(document)).toBeNull()
  })
})
