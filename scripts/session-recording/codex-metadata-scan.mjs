#!/usr/bin/env node

import { createHash } from 'node:crypto'
import { Buffer } from 'node:buffer'

export const SYNTHETIC_CANARY = 'codex-session-recording-canary'

function toBase64(value) {
  return Buffer.from(value, 'utf8').toString('base64')
}

function toHex(value) {
  return Buffer.from(value, 'utf8').toString('hex')
}

function toJsonEscaped(value) {
  return JSON.stringify(value).slice(1, -1)
}

function toUnicodeEscaped(value) {
  return [...value]
    .map((char) => `\\u${char.charCodeAt(0).toString(16).padStart(4, '0')}`)
    .join('')
}

export function buildSyntheticCanaryVariants(seed = SYNTHETIC_CANARY) {
  const sha256 = createHash('sha256').update(seed).digest('hex')
  return [
    { kind: 'raw', value: seed },
    { kind: 'sha256', value: sha256 },
    { kind: 'base64', value: toBase64(seed) },
    { kind: 'hex', value: toHex(seed) },
    { kind: 'urlencoded', value: encodeURIComponent(seed) },
    { kind: 'json_escaped', value: toJsonEscaped(seed) },
    { kind: 'unicode_escaped', value: toUnicodeEscaped(seed) },
  ]
}

export function scanTextForSyntheticCanary(text, seed = SYNTHETIC_CANARY) {
  const haystack = String(text ?? '')
  const findings = []
  for (const variant of buildSyntheticCanaryVariants(seed)) {
    if (haystack.includes(variant.value)) {
      findings.push(variant.kind)
    }
  }
  return findings
}

export function scanObjectForSyntheticCanary(value, seed = SYNTHETIC_CANARY) {
  return scanTextForSyntheticCanary(JSON.stringify(value), seed)
}
