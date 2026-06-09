#!/usr/bin/env node

import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'

function parseArgs(argv) {
  const args = { fixture: null, out: null }
  for (let index = 2; index < argv.length; index += 1) {
    const token = argv[index]
    if (token === '--fixture') {
      args.fixture = argv[index + 1]
      index += 1
      continue
    }
    if (token === '--out') {
      args.out = argv[index + 1]
      index += 1
      continue
    }
    throw new Error(`Unknown option: ${token}`)
  }
  if (!args.fixture || !args.out) {
    throw new Error('--fixture and --out are required')
  }
  return args
}

export function collectCodexPilotMetricsFromEvents(events) {
  const authoritativeUsage = events.find((event) =>
    event?.event_type === 'codex_usage_metadata' &&
    typeof event?.usage?.prompt_tokens === 'number' &&
    typeof event?.usage?.completion_tokens === 'number'
  )

  const manualEvents = events.filter((event) => event?.event_type === 'manual_intervention')
  const monotonicSamples = events
    .map((event) => event?.monotonic_ms)
    .filter((value) => typeof value === 'number')
  const latencyMs =
    monotonicSamples.length >= 2
      ? monotonicSamples[monotonicSamples.length - 1] - monotonicSamples[0]
      : null

  if (!authoritativeUsage) {
    return {
      token_usage: {
        availability: 'unavailable',
        source: 'none',
        prompt: null,
        completion: null,
        total: null,
      },
      latency_ms: latencyMs,
      latency_source: monotonicSamples.length >= 2 ? 'monotonic_event_clock' : 'unavailable',
      human_intervention_count: manualEvents.length,
      human_intervention_source: 'manual_event_ledger',
    }
  }

  const prompt = authoritativeUsage.usage.prompt_tokens
  const completion = authoritativeUsage.usage.completion_tokens
  return {
    token_usage: {
      availability: 'measured',
      source: 'codex_event_metadata',
      prompt,
      completion,
      total: prompt + completion,
    },
    latency_ms: latencyMs,
    latency_source: monotonicSamples.length >= 2 ? 'monotonic_event_clock' : 'unavailable',
    human_intervention_count: manualEvents.length,
    human_intervention_source: 'manual_event_ledger',
  }
}

function main() {
  const { fixture, out } = parseArgs(process.argv)
  const rawLines = readFileSync(resolve(fixture), 'utf8')
    .split(/\r?\n/)
    .filter(Boolean)
  const events = rawLines.map((line) => JSON.parse(line))
  const metrics = collectCodexPilotMetricsFromEvents(events)
  const outputPath = resolve(out)
  mkdirSync(dirname(outputPath), { recursive: true })
  writeFileSync(outputPath, JSON.stringify(metrics, null, 2))
}

main()
