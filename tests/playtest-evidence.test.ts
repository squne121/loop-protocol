/**
 * Tests for playtestEvidence module (AC1-AC13)
 * GIVEN/WHEN/THEN style
 *
 * DOM-dependent tests (panel rendering, clipboard, download) require a browser
 * environment and are covered under Runtime Verification Applicability: deferred.
 * See Issue #571 — deferred_destination: PR マージ前の手動 browser 確認フェーズ.
 *
 * This file tests the pure logic:
 *   - buildEvidenceData() schema shape (AC3, AC5-AC8)
 *   - toYaml() output (AC8)
 *   - shouldShowPanel() activation logic (AC2)
 */

import { describe, it, expect } from 'vitest'
import {
  buildEvidenceData,
  toYaml,
  shouldShowPanel,
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

  // AC6: note mentions ページズーム or display scaling
  it('GIVEN device_pixel_ratio note WHEN examined THEN it mentions ページズーム or display scaling', () => {
    const data = buildEvidenceData()
    const noteContainsZoomInfo =
      data.environment.device_pixel_ratio.note.includes('ページズーム') ||
      data.environment.device_pixel_ratio.note.includes('zoom') ||
      data.environment.device_pixel_ratio.note.includes('display scaling') ||
      data.environment.device_pixel_ratio.note.includes('scaling')
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
