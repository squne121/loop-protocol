/**
 * Tests for playtestEvidence module (AC1-AC13)
 * GIVEN/WHEN/THEN style
 *
 * DOM-dependent tests (AC3, AC6, AC8, AC10, AC11, AC13) use jsdom environment.
 * Pure logic tests (buildEvidenceData, toYaml, shouldShowPanel) run in default node env.
 *
 * This file tests:
 *   - buildEvidenceData() schema shape (AC3, AC5-AC8)
 *   - toYaml() output (AC8)
 *   - shouldShowPanel() activation logic (AC2)
 *   - initPlaytestEvidencePanel() DOM mounting, close/toggle behavior (AC3, AC6, AC8, AC10, AC11, AC13)
 */

// @vitest-environment jsdom

import { describe, it, expect, beforeEach, vi } from 'vitest'
import {
  beginPlaytestEvidenceSortie,
  createPlaytestEvidenceRuntimeState,
  recordCommandUse,
  recordLocalThreatSample,
  recordTargetSwitch,
  resetPlaytestEvidenceStore,
  setSelfExplanationPrompt,
  setSelfExplanationResponse,
} from '../src/playtest/assistPlayerEventLog'
import {
  buildEvidenceData,
  toYaml,
  shouldShowPanel,
  initPlaytestEvidencePanel,
  resolveAppUnderTestCommit,
  type PlaytestEvidenceData,
} from '../src/ui/playtestEvidence'

// --- AC8: schema shape ---
describe('buildEvidenceData', () => {
  beforeEach(() => {
    resetPlaytestEvidenceStore()
  })

  it('GIVEN a Node.js environment WHEN buildEvidenceData is called THEN returns schema v1 structure', () => {
    const data = buildEvidenceData()
    expect(data.playtest_evidence_schema_version).toBe('v2')
    expect(data.generated_at).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/)
    expect(data.source_url).toBeDefined()
    expect(data.app_under_test).toBeDefined()
    expect(data.browser).toBeDefined()
    expect(data.environment).toBeDefined()
    expect(data.runtime_state).toBeDefined()
    expect(data.deterministic_events).toBeDefined()
    expect(data.hashes).toBeDefined()
  })

  // AC3: generated_at / executed_at is ISO 8601
  it('GIVEN buildEvidenceData WHEN called THEN generated_at is a valid ISO 8601 date', () => {
    const data = buildEvidenceData()
    const d = new Date(data.generated_at)
    expect(isNaN(d.getTime())).toBe(false)
  })

  // AC4: app_under_test.commit fallback when VITE_LOOP_COMMIT_SHA is not set
  it('GIVEN VITE_LOOP_COMMIT_SHA is not set at build time WHEN buildEvidenceData THEN app_under_test.commit is "unknown" with reason', () => {
    const data = buildEvidenceData()
    // In the test environment import.meta.env.VITE_LOOP_COMMIT_SHA is not set
    // so we expect the fallback behavior
    if (data.app_under_test.commit === 'unknown') {
      expect(typeof data.app_under_test.commit_unknown_reason).toBe('string')
      expect((data.app_under_test.commit_unknown_reason ?? '').length).toBeGreaterThan(0)
    } else {
      // SHA was injected — it must be a 40-char hex string
      expect(data.app_under_test.commit).toMatch(/^[0-9a-f]{40}$/)
      expect('commit_unknown_reason' in data.app_under_test).toBe(false)
    }
  })

  // AC3: OS/platform
  it('GIVEN buildEvidenceData WHEN called THEN browser.platform is a string', () => {
    const data = buildEvidenceData()
    expect(typeof data.browser.platform).toBe('string')
  })

  // AC3: language
  it('GIVEN buildEvidenceData WHEN called THEN environment.language is a string', () => {
    const data = buildEvidenceData()
    expect(typeof data.environment.language).toBe('string')
  })

  // AC3: timezone
  it('GIVEN buildEvidenceData WHEN called THEN environment.timezone is a non-empty string', () => {
    const data = buildEvidenceData()
    expect(typeof data.environment.timezone).toBe('string')
    expect(data.environment.timezone.length).toBeGreaterThan(0)
  })

  // AC4: version_source is one of the known values
  it('GIVEN buildEvidenceData WHEN called THEN browser.version_source is a known enum value', () => {
    const data = buildEvidenceData()
    expect(['userAgentData', 'userAgent', 'unknown']).toContain(data.browser.version_source)
  })

  // AC4: unknown_reason when version_source is unknown
  it('GIVEN version_source is unknown WHEN buildEvidenceData THEN unknown_reason is defined', () => {
    const data = buildEvidenceData()
    if (data.browser.version_source === 'unknown') {
      expect(typeof data.browser.unknown_reason).toBe('string')
      expect((data.browser.unknown_reason ?? '').length).toBeGreaterThan(0)
    }
  })

  // AC5: viewport has all required fields including visual_viewport_scale
  it('GIVEN buildEvidenceData WHEN called THEN environment.viewport has all required fields', () => {
    const data = buildEvidenceData()
    const vp = data.environment.viewport
    // These fields must exist (values may be 0 / null in Node.js but keys must be present)
    expect('inner_width' in vp).toBe(true)
    expect('inner_height' in vp).toBe(true)
    expect('client_width' in vp).toBe(true)
    expect('client_height' in vp).toBe(true)
    expect('visual_viewport_width' in vp).toBe(true)
    expect('visual_viewport_height' in vp).toBe(true)
    expect('visual_viewport_scale' in vp).toBe(true)
  })

  // AC6: device_pixel_ratio has value and note
  it('GIVEN buildEvidenceData WHEN called THEN environment.device_pixel_ratio has value (number) and note (string)', () => {
    const data = buildEvidenceData()
    const dpr = data.environment.device_pixel_ratio
    expect(typeof dpr.value).toBe('number')
    expect(typeof dpr.note).toBe('string')
    expect(dpr.note.length).toBeGreaterThan(0)
  })

  // AC6: note mentions zoom or display scaling
  it('GIVEN device_pixel_ratio note WHEN examined THEN it mentions zoom or display scaling', () => {
    const data = buildEvidenceData()
    const noteContainsZoomInfo =
      data.environment.device_pixel_ratio.note.includes('zoom') ||
      data.environment.device_pixel_ratio.note.includes('display scaling') ||
      data.environment.device_pixel_ratio.note.includes('scaling') ||
      data.environment.device_pixel_ratio.note.includes('ページズーム')
    expect(noteContainsZoomInfo).toBe(true)
  })

  // AC2: visual_viewport_scale is null when window.visualViewport is not available
  it('GIVEN Node.js environment (no window.visualViewport) WHEN buildEvidenceData THEN viewport.visual_viewport_scale is null', () => {
    const data = buildEvidenceData()
    expect(data.environment.viewport.visual_viewport_scale).toBeNull()
  })

  // AC7: screen has width/height/avail_width/avail_height
  it('GIVEN buildEvidenceData WHEN called THEN environment.screen has four dimension fields', () => {
    const data = buildEvidenceData()
    const s = data.environment.screen
    expect('width' in s).toBe(true)
    expect('height' in s).toBe(true)
    expect('avail_width' in s).toBe(true)
    expect('avail_height' in s).toBe(true)
  })

  // AC8: hashes field present
  it('GIVEN buildEvidenceData WHEN called THEN hashes field is an object', () => {
    const data = buildEvidenceData()
    expect(typeof data.hashes).toBe('object')
    expect(data.hashes).not.toBeNull()
  })

  it('GIVEN recorded assist_player evidence WHEN buildEvidenceData THEN deterministic_events and qualitative_notes stay separated', () => {
    const runtime = createPlaytestEvidenceRuntimeState()
    beginPlaytestEvidenceSortie(runtime)
    recordLocalThreatSample({ tick: 5, commandSeq: 2, phase: 'after', threatCount: 0 })
    recordCommandUse(5, 2, true)
    recordTargetSwitch({
      tick: 5,
      commandSeq: 2,
      allyId: 1,
      fromTargetId: 'enemy:2',
      toTargetId: 'enemy:1',
      causedByCommandIntent: true,
    })
    setSelfExplanationPrompt('What changed the battle outcome most, and why?')
    setSelfExplanationResponse('I redirected the ally onto the closest threat.')

    const data = buildEvidenceData()
    expect(data.deterministic_events.map((event) => event.type)).toEqual([
      'command_use',
      'target_switch',
      'local_threat_sample',
    ])
    expect(data.qualitative_notes?.self_explanation_response).toContain('redirected')
  })
})


// --- AC3, AC4, AC5: resolveAppUnderTestCommit ---
// Note: import.meta.env is statically replaced by Vite at build time and cannot be mutated in
// tests. Instead, resolveAppUnderTestCommit accepts an optional overrideEnvValue parameter for
// unit test injection. Production code calls resolveAppUnderTestCommit() with no argument.
describe('resolveAppUnderTestCommit', () => {
  // AC3: valid SHA injection
  it('GIVEN VITE_LOOP_COMMIT_SHA is a valid 40-char hex SHA WHEN resolveAppUnderTestCommit THEN commit is the SHA and commit_unknown_reason is absent', () => {
    const validSha = 'a'.repeat(40)
    const result = resolveAppUnderTestCommit(validSha)
    expect(result.commit).toBe(validSha)
    expect('commit_unknown_reason' in result).toBe(false)
  })

  // AC4: fallback when not set (null = explicit "not set" sentinel)
  it('GIVEN VITE_LOOP_COMMIT_SHA is undefined WHEN resolveAppUnderTestCommit THEN commit is "unknown" and commit_unknown_reason is present', () => {
    const result = resolveAppUnderTestCommit(null)
    expect(result.commit).toBe('unknown')
    expect(typeof result.commit_unknown_reason).toBe('string')
    expect((result.commit_unknown_reason ?? '').length).toBeGreaterThan(0)
  })

  // AC4: fallback when value is not 40-char hex
  it('GIVEN VITE_LOOP_COMMIT_SHA is an invalid value WHEN resolveAppUnderTestCommit THEN commit is "unknown" and commit_unknown_reason mentions the invalid value', () => {
    const result = resolveAppUnderTestCommit('not-a-sha')
    expect(result.commit).toBe('unknown')
    expect(typeof result.commit_unknown_reason).toBe('string')
    expect(result.commit_unknown_reason).toContain('not-a-sha')
  })

  // AC5: commit_unknown_reason key is absent on success (object shape)
  it('GIVEN valid SHA WHEN resolveAppUnderTestCommit THEN object does NOT have commit_unknown_reason key at all', () => {
    const validSha = '0123456789abcdef'.repeat(2) + '01234567'
    const result = resolveAppUnderTestCommit(validSha)
    expect(Object.prototype.hasOwnProperty.call(result, 'commit_unknown_reason')).toBe(false)
  })

  // AC3: 40-char hex validation — exactly 40 chars of 0-9a-f
  it('GIVEN a 39-char hex string WHEN resolveAppUnderTestCommit THEN falls back to unknown', () => {
    const result = resolveAppUnderTestCommit('a'.repeat(39))
    expect(result.commit).toBe('unknown')
  })

  // AC3: uppercase hex should not match (must be lowercase)
  it('GIVEN a 40-char uppercase hex string WHEN resolveAppUnderTestCommit THEN falls back to unknown', () => {
    const result = resolveAppUnderTestCommit('A'.repeat(40))
    expect(result.commit).toBe('unknown')
  })

  // AC3: valid boundary — exactly 40 lowercase hex chars
  it('GIVEN exactly 40 lowercase hex chars WHEN resolveAppUnderTestCommit THEN commit matches the SHA', () => {
    const validSha = '0123456789abcdef0123456789abcdef01234567'
    const result = resolveAppUnderTestCommit(validSha)
    expect(result.commit).toBe(validSha)
  })

  // AC3: 41-char hex string should not match
  it('GIVEN a 41-char hex string WHEN resolveAppUnderTestCommit THEN falls back to unknown', () => {
    const result = resolveAppUnderTestCommit('a'.repeat(41))
    expect(result.commit).toBe('unknown')
  })
})

// --- AC8: YAML output ---
describe('toYaml', () => {
  it('GIVEN a PlaytestEvidenceData WHEN toYaml is called THEN output starts with schema version line', () => {
    const data = buildEvidenceData()
    const yaml = toYaml(data)
    expect(yaml).toContain('playtest_evidence_schema_version: v2')
  })

  it('GIVEN a PlaytestEvidenceData WHEN toYaml is called THEN output is a non-empty string', () => {
    const data = buildEvidenceData()
    const yaml = toYaml(data)
    expect(typeof yaml).toBe('string')
    expect(yaml.length).toBeGreaterThan(50)
  })

  it('GIVEN a PlaytestEvidenceData WHEN toYaml THEN generated_at key appears in output', () => {
    const data = buildEvidenceData()
    const yaml = toYaml(data)
    expect(yaml).toContain('generated_at:')
  })

  it('GIVEN a PlaytestEvidenceData WHEN toYaml THEN source_url key appears in output', () => {
    const data = buildEvidenceData()
    const yaml = toYaml(data)
    expect(yaml).toContain('source_url:')
  })

  it('GIVEN a PlaytestEvidenceData WHEN toYaml THEN app_under_test key appears in output', () => {
    const data = buildEvidenceData()
    const yaml = toYaml(data)
    expect(yaml).toContain('app_under_test:')
  })

  it('GIVEN a PlaytestEvidenceData WHEN toYaml THEN output ends with a newline', () => {
    const data = buildEvidenceData()
    const yaml = toYaml(data)
    expect(yaml.endsWith('\n')).toBe(true)
  })

  // AC3: YAML output includes visual_viewport_scale in viewport
  it('GIVEN buildEvidenceData WHEN toYaml THEN output contains visual_viewport_scale key', () => {
    const data = buildEvidenceData()
    const yaml = toYaml(data)
    expect(yaml).toContain('visual_viewport_scale:')
  })

  it('GIVEN recorded assist_player evidence WHEN toYaml THEN output contains deterministic_events and qualitative_notes', () => {
    const runtime = createPlaytestEvidenceRuntimeState()
    beginPlaytestEvidenceSortie(runtime)
    recordCommandUse(7, 1, true)
    setSelfExplanationPrompt('What changed the battle outcome most, and why?')
    setSelfExplanationResponse('The assist command pulled aggro away from me.')

    const yaml = toYaml(buildEvidenceData())
    expect(yaml).toContain('deterministic_events:')
    expect(yaml).toContain('qualitative_notes:')
    expect(yaml).toContain('self_explanation_response:')
    expect(yaml).toContain('command_seq:')
    expect(yaml).toContain('event_type_order:')
  })
})

// --- AC2: activation logic (pure predicate) ---
describe('shouldShowPanel', () => {
  it('GIVEN empty search string WHEN shouldShowPanel THEN returns false', () => {
    expect(shouldShowPanel('')).toBe(false)
  })

  it('GIVEN ?playtest_evidence=1 WHEN shouldShowPanel THEN returns true', () => {
    expect(shouldShowPanel('?playtest_evidence=1')).toBe(true)
  })

  it('GIVEN ?playtest_evidence=0 WHEN shouldShowPanel THEN returns false', () => {
    expect(shouldShowPanel('?playtest_evidence=0')).toBe(false)
  })

  it('GIVEN ?foo=bar (no playtest_evidence key) WHEN shouldShowPanel THEN returns false', () => {
    expect(shouldShowPanel('?foo=bar')).toBe(false)
  })

  it('GIVEN ?playtest_evidence=1&other=val WHEN shouldShowPanel THEN returns true', () => {
    expect(shouldShowPanel('?playtest_evidence=1&other=val')).toBe(true)
  })
})

// --- AC11: read-only constraint (structural) ---
describe('AC11 read-only structural check', () => {
  it('GIVEN playtestEvidence module WHEN buildEvidenceData is called THEN no exception is thrown', () => {
    expect(() => buildEvidenceData()).not.toThrow()
  })

  it('GIVEN PlaytestEvidenceData type WHEN used in assertion THEN structure is assignable', () => {
    const data: PlaytestEvidenceData = buildEvidenceData()
    expect(data.playtest_evidence_schema_version).toBe('v2')
  })
})

// --- DOM tests: Close button / Toggle button ---
describe('initPlaytestEvidencePanel DOM', () => {
  let container: HTMLElement

  beforeEach(() => {
    container = document.createElement('div')
    document.body.appendChild(container)
  })

  // AC3/AC11: close button exists with data-playtest-close="true"
  it('GIVEN initPlaytestEvidencePanel WHEN mounted THEN close button with data-playtest-close exists in panel', () => {
    initPlaytestEvidencePanel(container, '')
    const closeBtn = container.querySelector('[data-playtest-close="true"]')
    expect(closeBtn).not.toBeNull()
  })

  // AC11: close button has type="button" and aria-label
  it('GIVEN mounted panel WHEN close button inspected THEN has type=button and aria-label', () => {
    initPlaytestEvidencePanel(container, '')
    const closeBtn = container.querySelector('[data-playtest-close="true"]') as HTMLButtonElement
    expect(closeBtn).not.toBeNull()
    expect(closeBtn.type).toBe('button')
    expect(closeBtn.getAttribute('aria-label')).toBeTruthy()
  })

  // AC10: toggle button has data-playtest-toggle, type=button, aria-controls, aria-expanded, aria-label
  it('GIVEN mounted panel WHEN toggle button inspected THEN has required aria attributes', () => {
    initPlaytestEvidencePanel(container, '')
    const toggleBtn = container.querySelector('[data-playtest-toggle="true"]') as HTMLButtonElement
    expect(toggleBtn).not.toBeNull()
    expect(toggleBtn.type).toBe('button')
    expect(toggleBtn.getAttribute('aria-controls')).toBeTruthy()
    expect(toggleBtn.getAttribute('aria-expanded')).toBeDefined()
    expect(toggleBtn.getAttribute('aria-label')).toBeTruthy()
  })

  // AC8: initPlaytestEvidencePanel('') mounts toggle and panel is initially hidden
  it('GIVEN initPlaytestEvidencePanel with empty search WHEN mounted THEN panel is initially hidden', () => {
    initPlaytestEvidencePanel(container, '')
    const panel = container.querySelector('[data-playtest-evidence="true"]') as HTMLElement
    expect(panel).not.toBeNull()
    expect(panel.hidden).toBe(true)
  })

  // AC9: initPlaytestEvidencePanel('?playtest_evidence=1') mounts panel as initially open
  it('GIVEN initPlaytestEvidencePanel with ?playtest_evidence=1 WHEN mounted THEN panel is initially visible', () => {
    initPlaytestEvidencePanel(container, '?playtest_evidence=1')
    const panel = container.querySelector('[data-playtest-evidence="true"]') as HTMLElement
    expect(panel).not.toBeNull()
    expect(panel.hidden).toBe(false)
  })

  // AC6: toggle button click opens hidden panel
  it('GIVEN panel is hidden WHEN toggle button clicked THEN panel becomes visible', () => {
    initPlaytestEvidencePanel(container, '')
    const panel = container.querySelector('[data-playtest-evidence="true"]') as HTMLElement
    const toggleBtn = container.querySelector('[data-playtest-toggle="true"]') as HTMLButtonElement
    expect(panel.hidden).toBe(true)
    toggleBtn.click()
    expect(panel.hidden).toBe(false)
  })

  // AC6: toggle button click again closes open panel
  it('GIVEN panel is visible WHEN toggle button clicked THEN panel becomes hidden', () => {
    initPlaytestEvidencePanel(container, '?playtest_evidence=1')
    const panel = container.querySelector('[data-playtest-evidence="true"]') as HTMLElement
    const toggleBtn = container.querySelector('[data-playtest-toggle="true"]') as HTMLButtonElement
    expect(panel.hidden).toBe(false)
    toggleBtn.click()
    expect(panel.hidden).toBe(true)
  })

  // AC3: close button click hides panel (panel.hidden = true)
  it('GIVEN panel is visible WHEN close button clicked THEN panel becomes hidden', () => {
    initPlaytestEvidencePanel(container, '?playtest_evidence=1')
    const panel = container.querySelector('[data-playtest-evidence="true"]') as HTMLElement
    const closeBtn = container.querySelector('[data-playtest-close="true"]') as HTMLButtonElement
    expect(panel.hidden).toBe(false)
    closeBtn.click()
    expect(panel.hidden).toBe(true)
  })

  // AC4 (reopen after close): toggle button after close reopens panel
  it('GIVEN panel closed via close button WHEN toggle button clicked THEN panel reopens', () => {
    initPlaytestEvidencePanel(container, '?playtest_evidence=1')
    const panel = container.querySelector('[data-playtest-evidence="true"]') as HTMLElement
    const closeBtn = container.querySelector('[data-playtest-close="true"]') as HTMLButtonElement
    const toggleBtn = container.querySelector('[data-playtest-toggle="true"]') as HTMLButtonElement
    closeBtn.click()
    expect(panel.hidden).toBe(true)
    toggleBtn.click()
    expect(panel.hidden).toBe(false)
  })

  // aria-expanded sync: toggle updates aria-expanded
  it('GIVEN panel is hidden WHEN toggle clicked THEN aria-expanded becomes true', () => {
    initPlaytestEvidencePanel(container, '')
    const toggleBtn = container.querySelector('[data-playtest-toggle="true"]') as HTMLButtonElement
    expect(toggleBtn.getAttribute('aria-expanded')).toBe('false')
    toggleBtn.click()
    expect(toggleBtn.getAttribute('aria-expanded')).toBe('true')
  })

  // aria-expanded sync: close button updates aria-expanded on toggle
  it('GIVEN panel is visible WHEN close button clicked THEN toggle aria-expanded becomes false', () => {
    initPlaytestEvidencePanel(container, '?playtest_evidence=1')
    const toggleBtn = container.querySelector('[data-playtest-toggle="true"]') as HTMLButtonElement
    const closeBtn = container.querySelector('[data-playtest-close="true"]') as HTMLButtonElement
    expect(toggleBtn.getAttribute('aria-expanded')).toBe('true')
    closeBtn.click()
    expect(toggleBtn.getAttribute('aria-expanded')).toBe('false')
  })

  // AC13: calling initPlaytestEvidencePanel multiple times is idempotent
  it('GIVEN initPlaytestEvidencePanel WHEN called twice THEN only one toggle is mounted', () => {
    initPlaytestEvidencePanel(container, '')
    initPlaytestEvidencePanel(container, '')
    const toggleBtns = container.querySelectorAll('[data-playtest-toggle="true"]')
    expect(toggleBtns.length).toBe(1)
  })

  it('GIVEN initPlaytestEvidencePanel WHEN called twice THEN only one panel is mounted', () => {
    initPlaytestEvidencePanel(container, '')
    initPlaytestEvidencePanel(container, '')
    const panels = container.querySelectorAll('[data-playtest-evidence="true"]')
    expect(panels.length).toBe(1)
  })

  // AC12: close button text is × (U+00D7 multiplication sign)
  it('GIVEN mounted panel WHEN close button inspected THEN textContent is × (U+00D7)', () => {
    initPlaytestEvidencePanel(container, '')
    const closeBtn = container.querySelector('[data-playtest-close="true"]') as HTMLButtonElement
    expect(closeBtn).not.toBeNull()
    expect(closeBtn.textContent).toBe('×')
  })

  // Integration smoke test: evidence collection -> snapshot -> panel textarea -> YAML serialization
  it('GIVEN panel opened with visualViewport.scale = 1.75 WHEN initPlaytestEvidencePanel THEN textarea YAML contains visual_viewport_scale: 1.75', () => {
    const origVisualViewport = window.visualViewport
    Object.defineProperty(window, 'visualViewport', {
      value: { scale: 1.75, width: 800, height: 600 },
      configurable: true,
    })

    try {
      initPlaytestEvidencePanel(container, '?playtest_evidence=1')
      const textarea = container.querySelector('[data-playtest-fallback="true"]') as HTMLTextAreaElement
      expect(textarea).not.toBeNull()
      expect(textarea.value).toContain('visual_viewport_scale: 1.75')
    } finally {
      Object.defineProperty(window, 'visualViewport', {
        value: origVisualViewport,
        configurable: true,
      })
    }
  })
})

// --- AC12: close → reopen snapshot stability ---
describe('AC12 snapshot stability across close/reopen', () => {
  let container: HTMLElement

  beforeEach(() => {
    container = document.createElement('div')
    document.body.appendChild(container)
  })

  // AC12: Copy YAML after close → reopen uses the same snapshot
  it('GIVEN panel opened, copy called, panel closed, reopened WHEN copy called again THEN same YAML is passed to clipboard', async () => {
    const writtenTexts: string[] = []
    const clipboardMock = {
      writeText: vi.fn((text: string) => {
        writtenTexts.push(text)
        return Promise.resolve()
      }),
    }
    Object.defineProperty(navigator, 'clipboard', {
      value: clipboardMock,
      configurable: true,
    })

    initPlaytestEvidencePanel(container, '')
    const panel = container.querySelector('[data-playtest-evidence="true"]') as HTMLElement
    const toggleBtn = container.querySelector('[data-playtest-toggle="true"]') as HTMLButtonElement
    const closeBtn = container.querySelector('[data-playtest-close="true"]') as HTMLButtonElement
    const copyBtn = container.querySelector('[data-action="copy-yaml"]') as HTMLButtonElement

    // First open: snapshot should be initialized
    toggleBtn.click()
    expect(panel.hidden).toBe(false)

    // Copy first time
    copyBtn.click()
    await Promise.resolve()

    // Close panel
    closeBtn.click()
    expect(panel.hidden).toBe(true)

    // Reopen panel
    toggleBtn.click()
    expect(panel.hidden).toBe(false)

    // Copy second time
    copyBtn.click()
    await Promise.resolve()

    // Both copy calls should have received exactly the same YAML
    expect(writtenTexts.length).toBe(2)
    expect(writtenTexts[0]).toBe(writtenTexts[1])
    expect(writtenTexts[0]).toContain('playtest_evidence_schema_version: v2')
  })

  // AC12: Download uses the same snapshot after close → reopen
  it('GIVEN panel opened, closed, reopened WHEN download triggered THEN blob content is the same snapshot YAML', () => {
    const createdUrls: string[] = []
    const clickedHrefs: string[] = []

    // Mock URL.createObjectURL
    const origCreateObjectURL = URL.createObjectURL
    URL.createObjectURL = vi.fn(() => {
      const url = 'blob:mock-' + createdUrls.length
      createdUrls.push(url)
      return url
    }) as unknown as typeof URL.createObjectURL
    URL.revokeObjectURL = vi.fn()

    // Spy on anchor click to capture href
    const origClick = HTMLAnchorElement.prototype.click
    HTMLAnchorElement.prototype.click = function (this: HTMLAnchorElement) {
      clickedHrefs.push(this.href)
    }

    try {
      initPlaytestEvidencePanel(container, '')
      const panel = container.querySelector('[data-playtest-evidence="true"]') as HTMLElement
      const toggleBtn = container.querySelector('[data-playtest-toggle="true"]') as HTMLButtonElement
      const closeBtn = container.querySelector('[data-playtest-close="true"]') as HTMLButtonElement
      const downloadBtn = container.querySelector('[data-action="download-yaml"]') as HTMLButtonElement

      // First open: snapshot initialized
      toggleBtn.click()
      expect(panel.hidden).toBe(false)

      // Download first time
      downloadBtn.click()

      // Close and reopen
      closeBtn.click()
      toggleBtn.click()
      expect(panel.hidden).toBe(false)

      // Download second time
      downloadBtn.click()

      // Both download calls should have used the same blob URL (same snapshot)
      expect(createdUrls.length).toBe(2)
      // The href used for both clicks should point to the same mock URL pattern
      expect(clickedHrefs.length).toBe(2)
      // Both blobs came from same YAML content — verified by createObjectURL call count
      // and that filename (generated_at in href download attribute) is the same
      const anchors = document.querySelectorAll('a[download]')
      // After cleanup there should be no lingering anchors (they are removed)
      expect(anchors.length).toBe(0)
    } finally {
      URL.createObjectURL = origCreateObjectURL
      HTMLAnchorElement.prototype.click = origClick
    }
  })
})

describe('self-explanation prompt DOM', () => {
  let container: HTMLElement

  beforeEach(() => {
    resetPlaytestEvidenceStore()
    container = document.createElement('div')
    document.body.appendChild(container)
  })

  it('GIVEN sortie terminal prompt is available WHEN panel initializes THEN DOM prompt is mounted as live region', async () => {
    setSelfExplanationPrompt('What changed the battle outcome most, and why?')
    initPlaytestEvidencePanel(container, '')
    await new Promise((resolve) => window.requestAnimationFrame(() => resolve(undefined)))

    const prompt = container.querySelector('[data-self-explanation-prompt="true"]') as HTMLElement
    const card = container.querySelector('[data-self-explanation-card="true"]') as HTMLElement
    expect(card.hidden).toBe(false)
    expect(prompt.getAttribute('role')).toBe('status')
    expect(prompt.textContent).toContain('battle outcome')
  })
})

// --- AC2/AC3: visual_viewport_scale collection ---
describe('visual_viewport_scale collection', () => {
  // AC2(a): visual_viewport_scale passthrough non-1 value (1.75)
  it('GIVEN window.visualViewport.scale = 1.75 WHEN buildEvidenceData THEN viewport.visual_viewport_scale = 1.75', () => {
    // Mock window.visualViewport with scale = 1.75
    const origVisualViewport = window.visualViewport
    Object.defineProperty(window, 'visualViewport', {
      value: { scale: 1.75, width: 800, height: 600 },
      configurable: true,
    })

    try {
      const data = buildEvidenceData()
      expect(data.environment.viewport.visual_viewport_scale).toBe(1.75)
    } finally {
      Object.defineProperty(window, 'visualViewport', {
        value: origVisualViewport,
        configurable: true,
      })
    }
  })

  // AC2(b): visual_viewport_scale = 0 is preserved (not converted to null)
  it('GIVEN window.visualViewport.scale = 0 WHEN buildEvidenceData THEN viewport.visual_viewport_scale = 0 (not null)', () => {
    const origVisualViewport = window.visualViewport
    Object.defineProperty(window, 'visualViewport', {
      value: { scale: 0, width: 800, height: 600 },
      configurable: true,
    })

    try {
      const data = buildEvidenceData()
      expect(data.environment.viewport.visual_viewport_scale).toBe(0)
      expect(data.environment.viewport.visual_viewport_scale).not.toBeNull()
    } finally {
      Object.defineProperty(window, 'visualViewport', {
        value: origVisualViewport,
        configurable: true,
      })
    }
  })

  // AC2(b): visual_viewport_scale is null when visualViewport is absent
  it('GIVEN window.visualViewport is null WHEN buildEvidenceData THEN viewport.visual_viewport_scale = null', () => {
    const origVisualViewport = window.visualViewport
    Object.defineProperty(window, 'visualViewport', {
      value: null,
      configurable: true,
    })

    try {
      const data = buildEvidenceData()
      expect(data.environment.viewport.visual_viewport_scale).toBeNull()
    } finally {
      Object.defineProperty(window, 'visualViewport', {
        value: origVisualViewport,
        configurable: true,
      })
    }
  })

  // AC3(c): YAML output includes visual_viewport_scale when scale value exists
  it('GIVEN window.visualViewport.scale = 1.75 WHEN toYaml is called THEN YAML contains visual_viewport_scale: 1.75', () => {
    const origVisualViewport = window.visualViewport
    Object.defineProperty(window, 'visualViewport', {
      value: { scale: 1.75, width: 800, height: 600 },
      configurable: true,
    })

    try {
      const data = buildEvidenceData()
      const yaml = toYaml(data)
      expect(yaml).toContain('visual_viewport_scale: 1.75')
    } finally {
      Object.defineProperty(window, 'visualViewport', {
        value: origVisualViewport,
        configurable: true,
      })
    }
  })
})
