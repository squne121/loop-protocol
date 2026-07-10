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

function parseNumericText(rawValue) {
  const normalized = `${rawValue ?? ''}`.trim().replace(/[^\d.-]/g, '')
  if (!normalized) {
    return 0
  }
  const parsed = Number.parseFloat(normalized)
  return Number.isFinite(parsed) ? parsed : 0
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
    const commandRail = globalThis.document.querySelector('aside.command-rail')
    if (
      !(appShell instanceof globalThis.HTMLElement) ||
      !(commandRail instanceof globalThis.HTMLElement)
    ) {
      throw new Error('Required overlay layout elements are missing.')
    }

    const shellStyle = globalThis.window.getComputedStyle(appShell)
    const railStyle = globalThis.window.getComputedStyle(commandRail)

    return {
      battleLayout: appShell.getAttribute('data-battle-layout'),
      gridTemplateColumns: shellStyle.gridTemplateColumns,
      gridTemplateColumnCount: shellStyle.gridTemplateColumns
        .trim()
        .split(/\s+/)
        .filter(Boolean).length,
      commandRailHiddenAttribute: commandRail.hasAttribute('hidden'),
      commandRailAriaHidden: commandRail.getAttribute('aria-hidden'),
      commandRailDisplay: railStyle.display,
      commandRailVisibility: railStyle.visibility,
      commandRailPointerEvents: railStyle.pointerEvents,
      commandRailWidth: Math.round(commandRail.getBoundingClientRect().width),
      interactiveDescendantCount: commandRail.querySelectorAll(
        'button, a, input, select, textarea, [data-action], [data-battle-interactive="true"]',
      ).length,
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

  const shotsField = frame.locator('[data-field="shots"]')
  const shotsBefore = parseNumericText(await shotsField.textContent())

  const canvas = frame.locator('canvas.battle-stage__canvas')
  await canvas.waitFor({ state: 'visible', timeout: timeoutMs })

  const canvasBox = await canvas.boundingBox()
  if (!canvasBox) {
    throw new Error('Canvas bounding box is unavailable for pointer-input smoke.')
  }

  const pointerX = canvasBox.x + Math.min(200, canvasBox.width / 2)
  const pointerY = canvasBox.y + Math.min(160, canvasBox.height / 2)

  let shotsAfter = shotsBefore
  for (let attempt = 0; attempt < 3; attempt += 1) {
    await page.mouse.move(pointerX, pointerY)
    await page.mouse.down({ button: 'left' })
    shotsAfter = await poll(
      `shots increased after canvas pointer hold ${attempt + 1}`,
      async () => parseNumericText(await shotsField.textContent()),
      (value) => value > shotsBefore,
      2_000,
    ).catch(() => shotsBefore)
    await page.mouse.up({ button: 'left' })
    if (shotsAfter > shotsBefore) {
      break
    }
  }

  if (shotsAfter <= shotsBefore) {
    throw new Error(
      `Canvas pointer input did not increase shots HUD value. before=${shotsBefore} after=${shotsAfter}`,
    )
  }

  return {
    shotsBefore,
    shotsAfter,
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
      (value) =>
        value.battleLayout === 'overlay-hud' &&
        value.gridTemplateColumnCount === 1 &&
        value.interactiveDescendantCount === 0 &&
        (value.commandRailHiddenAttribute === true || value.commandRailWidth === 0),
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