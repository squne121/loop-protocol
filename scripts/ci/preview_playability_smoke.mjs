import fs from 'node:fs/promises'
import path from 'node:path'
import process from 'node:process'

import { chromium } from '@playwright/test'

const REQUIRED_VIEWPORTS = [
  { width: 1437, height: 1365 },
  { width: 956, height: 1032 },
]

const DEFAULT_TIMEOUT_MS = 20_000
const DEFAULT_IFRAME_SANDBOX = 'allow-scripts allow-same-origin'

function parseArgs(argv) {
  const options = {
    url: '',
    outputDir: 'artifacts/preview-playability-smoke',
    timeoutMs: DEFAULT_TIMEOUT_MS,
    iframeSandbox: DEFAULT_IFRAME_SANDBOX,
  }

  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index]
    if (arg === '--url') {
      options.url = argv[index + 1] ?? ''
      index += 1
      continue
    }
    if (arg === '--output-dir') {
      options.outputDir = argv[index + 1] ?? options.outputDir
      index += 1
      continue
    }
    if (arg === '--timeout-ms') {
      const raw = argv[index + 1] ?? ''
      const timeoutMs = Number.parseInt(raw, 10)
      if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
        throw new Error(`Invalid --timeout-ms value: ${raw}`)
      }
      options.timeoutMs = timeoutMs
      index += 1
      continue
    }
    if (arg === '--iframe-sandbox') {
      options.iframeSandbox = argv[index + 1] ?? DEFAULT_IFRAME_SANDBOX
      index += 1
      continue
    }
    throw new Error(`Unknown argument: ${arg}`)
  }

  if (!options.url) {
    throw new Error('Missing required --url')
  }

  return options
}

function normalizeUrl(rawUrl) {
  const url = new globalThis.URL(rawUrl)
  if (!url.pathname.endsWith('/')) {
    url.pathname = `${url.pathname}/`
  }
  return url.toString()
}

async function ensureDirectory(dirPath) {
  await fs.mkdir(dirPath, { recursive: true })
}

async function poll(label, callback, predicate, timeoutMs) {
  const startedAt = Date.now()
  let lastValue
  while (Date.now() - startedAt <= timeoutMs) {
    lastValue = await callback()
    if (predicate(lastValue)) {
      return lastValue
    }
    await new Promise((resolve) => {
      globalThis.setTimeout(resolve, 250)
    })
  }
  throw new Error(
    `${label} did not satisfy predicate within ${timeoutMs}ms; last value=${JSON.stringify(lastValue)}`,
  )
}

async function captureFailureScreenshot(page, outputPath) {
  try {
    await page.screenshot({ path: outputPath, fullPage: true })
  } catch {
    // Best-effort evidence only.
  }
}

async function collectLayoutEvidence(frame) {
  return frame.evaluate(() => {
    const appShell = globalThis.document.querySelector('.app-shell')
    if (!(appShell instanceof globalThis.HTMLElement)) {
      throw new Error('Required overlay layout elements are missing.')
    }

    const shellStyle = globalThis.window.getComputedStyle(appShell)

    return {
      gridTemplateColumns: shellStyle.gridTemplateColumns,
      gridTemplateColumnCount: shellStyle.gridTemplateColumns
        .trim()
        .split(/\s+/)
        .filter(Boolean).length,
      // Issue #1377: the legacy `.command-rail` element is removed entirely
      // -- its absence from the DOM is the required-element check now,
      // replacing the previous width/visibility/interactive-descendant
      // assertions against a placeholder rail that no longer exists.
      commandRailPresent: globalThis.document.querySelector('aside.command-rail') !== null,
    }
  })
}

async function runPlayabilityFlow(page, frame, timeoutMs) {
  const beginNewRun = frame.locator('button[data-action="new-game"]')
  await beginNewRun.waitFor({ state: 'visible', timeout: timeoutMs })
  await beginNewRun.click()

  const launchSortie = frame.locator('button[data-action="start-sortie"]')
  await poll(
    'Launch sortie enabled',
    async () => launchSortie.isEnabled(),
    (value) => value === true,
    timeoutMs,
  )
  await launchSortie.click()

  const sortieStatus = frame.locator('[data-field="sortie-status"]')
  await poll(
    'sortie-status running',
    async () => (await sortieStatus.textContent()) ?? '',
    (value) => /In Progress|戦闘|Review ready|Area secured/i.test(value),
    timeoutMs,
  )

  // Issue #1375 AC2 removed the raw shots telemetry field
  // (`[data-field="shots"]`) from the combat HUD. Use the weapon readiness
  // field (`[data-field="combat-hud-weapon"]`, `src/ui/combatHud.ts`) instead:
  // it transitions "Ready" -> "Recharging" immediately after a shot fires, so
  // it still gives an observable signal that canvas pointer input reached
  // gameplay logic.
  const weaponField = frame.locator('[data-field="combat-hud-weapon"]')
  const weaponStateBefore = ((await weaponField.textContent()) ?? '').trim()

  const canvas = frame.locator('canvas.battle-stage__canvas')
  await canvas.waitFor({ state: 'visible', timeout: timeoutMs })

  const canvasBox = await canvas.boundingBox()
  if (!canvasBox) {
    throw new Error('Canvas bounding box is unavailable for pointer-input smoke.')
  }

  const pointerX = canvasBox.x + Math.min(200, canvasBox.width / 2)
  const pointerY = canvasBox.y + Math.min(160, canvasBox.height / 2)

  let weaponStateAfter = weaponStateBefore
  for (let attempt = 0; attempt < 3; attempt += 1) {
    await page.mouse.move(pointerX, pointerY)
    await page.mouse.down({ button: 'left' })
    weaponStateAfter = await poll(
      `weapon state changed after canvas pointer hold ${attempt + 1}`,
      async () => ((await weaponField.textContent()) ?? '').trim(),
      (value) => value !== '' && value !== weaponStateBefore,
      2_000,
    ).catch(() => weaponStateBefore)
    await page.mouse.up({ button: 'left' })
    if (weaponStateAfter !== weaponStateBefore) {
      break
    }
  }

  if (weaponStateAfter === weaponStateBefore) {
    throw new Error(
      `Canvas pointer input did not change combat HUD weapon state. before=${weaponStateBefore} after=${weaponStateAfter}`,
    )
  }

  return {
    weaponStateBefore,
    weaponStateAfter,
    sortieStatus: (await sortieStatus.textContent())?.trim() ?? '',
  }
}

async function runScenario(browser, options, mode, viewport) {
  const context = await browser.newContext({
    viewport,
    screen: viewport,
  })
  const page = await context.newPage()
  const consoleMessages = []
  const pageErrors = []

  page.on('console', (message) => {
    consoleMessages.push(`${message.type()}: ${message.text()}`)
  })
  page.on('pageerror', (error) => {
    pageErrors.push(error.message)
  })

  const viewportLabel = `${viewport.width}x${viewport.height}`
  const screenshotPath = path.join(options.outputDir, `${mode}-${viewportLabel}.png`)
  const failureScreenshotPath = path.join(
    options.outputDir,
    `${mode}-${viewportLabel}-failure.png`,
  )

  try {
    let frame = page.mainFrame()

    if (mode === 'direct') {
      await page.goto(options.url, { waitUntil: 'networkidle', timeout: options.timeoutMs })
    } else {
      await page.setContent(
        `<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>Preview iframe wrapper</title>
    <style>
      html, body {
        margin: 0;
        width: 100%;
        height: 100%;
        background: #0b0f14;
      }
      iframe {
        display: block;
        width: 100%;
        height: 100%;
        border: 0;
      }
    </style>
  </head>
  <body>
    <iframe
      data-preview-frame
      src="${options.url}"
      sandbox="${options.iframeSandbox}"
      referrerpolicy="no-referrer"
    ></iframe>
  </body>
</html>`,
        { waitUntil: 'domcontentloaded', timeout: options.timeoutMs },
      )
      const frameHandle = await page.locator('iframe[data-preview-frame]').elementHandle()
      if (!frameHandle) {
        throw new Error('Failed to create preview iframe wrapper.')
      }
      frame = await poll(
        'iframe content frame',
        async () => frameHandle.contentFrame(),
        (value) => value !== null,
        options.timeoutMs,
      )
      await frame.waitForLoadState('networkidle', { timeout: options.timeoutMs })
    }

    const layout = await poll(
      `${mode} overlay layout evidence`,
      async () => collectLayoutEvidence(frame),
      (value) => value.gridTemplateColumnCount === 1 && value.commandRailPresent === false,
      options.timeoutMs,
    )

    const playability = await runPlayabilityFlow(page, frame, options.timeoutMs)
    await page.screenshot({ path: screenshotPath, fullPage: true })

    return {
      mode,
      viewport,
      status: 'pass',
      screenshot: path.relative(options.outputDir, screenshotPath),
      failure_screenshot: null,
      layout,
      playability,
      console_messages: consoleMessages,
      page_errors: pageErrors,
    }
  } catch (error) {
    await captureFailureScreenshot(page, failureScreenshotPath)
    return {
      mode,
      viewport,
      status: 'fail',
      screenshot: null,
      failure_screenshot: path.relative(options.outputDir, failureScreenshotPath),
      error: error instanceof Error ? error.message : String(error),
      console_messages: consoleMessages,
      page_errors: pageErrors,
    }
  } finally {
    await context.close()
  }
}

async function main() {
  const options = parseArgs(process.argv.slice(2))
  options.url = normalizeUrl(options.url)
  options.outputDir = path.resolve(options.outputDir)
  await ensureDirectory(options.outputDir)

  const browser = await chromium.launch({ headless: true })
  const summary = {
    schema: 'preview_playability_smoke/v1',
    generated_at: new Date().toISOString(),
    preview_url: options.url,
    iframe_sandbox: options.iframeSandbox,
    required_viewports: REQUIRED_VIEWPORTS,
    results: [],
  }

  try {
    for (const mode of ['direct', 'iframe']) {
      for (const viewport of REQUIRED_VIEWPORTS) {
        const result = await runScenario(browser, options, mode, viewport)
        summary.results.push(result)
      }
    }
  } finally {
    await browser.close()
  }

  const summaryPath = path.join(options.outputDir, 'summary.json')
  await fs.writeFile(summaryPath, `${JSON.stringify(summary, null, 2)}\n`, 'utf8')

  const failedResults = summary.results.filter((result) => result.status !== 'pass')
  if (failedResults.length > 0) {
    const failedModes = failedResults
      .map((result) => `${result.mode}:${result.viewport.width}x${result.viewport.height}`)
      .join(', ')
    throw new Error(`Preview playability smoke failed for ${failedModes}. See ${summaryPath}`)
  }

  process.stdout.write(`${summaryPath}\n`)
}

main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.stack ?? error.message : String(error)}\n`)
  process.exitCode = 1
})