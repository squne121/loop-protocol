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

import { describe, it, expect, beforeEach } from 'vitest'
import {
  buildEvidenceData,
  toYaml,
  shouldShowPanel,
  initPlaytestEvidencePanel,
  type PlaytestEvidenceData,
} from '../src/ui/playtestEvidence'

// --- AC8: schema shape ---
describe('buildEvidenceData', () => {
  it('GIVEN a Node.js environment WHEN buildEvidenceData is called THEN returns schema v1 structure', () => {
    const data = buildEvidenceData()
    expect(data.playtest_evidence_schema_version).toBe('v1')
    expect(data.generated_at).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/)
    expect(data.source_url).toBeDefined()
    expect(data.app_under_test).toBeDefined()
    expect(data.browser).toBeDefined()
    expect(data.environment).toBeDefined()
    expect(data.hashes).toBeDefined()
  })

  // AC3: generated_at / executed_at is ISO 8601
  it('GIVEN buildEvidenceData WHEN called THEN generated_at is a valid ISO 8601 date', () => {
    const data = buildEvidenceData()
    const d = new Date(data.generated_at)
    expect(isNaN(d.getTime())).toBe(false)
  })

  // AC8: app_under_test.commit is unknown with reason
  it('GIVEN Pages context WHEN buildEvidenceData THEN app_under_test.commit is "unknown" with reason', () => {
    const data = buildEvidenceData()
    expect(data.app_under_test.commit).toBe('unknown')
    expect(typeof data.app_under_test.commit_unknown_reason).toBe('string')
    expect(data.app_under_test.commit_unknown_reason.length).toBeGreaterThan(0)
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

  // AC5: viewport has all 6 required fields
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
})

// --- AC8: YAML output ---
describe('toYaml', () => {
  it('GIVEN a PlaytestEvidenceData WHEN toYaml is called THEN output starts with schema version line', () => {
    const data = buildEvidenceData()
    const yaml = toYaml(data)
    expect(yaml).toContain('playtest_evidence_schema_version: v1')
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
    expect(data.playtest_evidence_schema_version).toBe('v1')
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
})
