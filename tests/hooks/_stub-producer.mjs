#!/usr/bin/env node
/**
 * _stub-producer.mjs
 *
 * Test stub for the session manifest producer.
 * Returns a minimal valid manifest JSON that the composite hook can use.
 * Used via CODEX_SESSION_RECORDING_PRODUCER env var override in tests.
 */

/* global process */

const stubManifest = {
  schema: 'agent_session_manifest/v1',
  manifest_id: 'stub-manifest-test-001',
  generated_at: new Date().toISOString(),
  repository: 'squne121/loop-protocol',
  phase: {
    main_loop: 'impl',
    ledger_phase: 'post_commit_verification',
    instance_id: 'test-stub:impl:001',
  },
  actor: {
    type: 'ai_agent',
    name: 'stub-hook',
    session_id: null,
  },
  evidence: {
    source_kind: 'artifact',
    source_ref: 'artifacts/stub-manifest-test.json',
    visibility: 'private_artifact',
  },
  secret_policy: {
    runtime_boundary: {
      attested: false,
      evidence_ref: null,
    },
    no_secret_in_output: true,
  },
  token_usage: {
    availability: 'unavailable',
    source: 'none',
    prompt: null,
    completion: null,
    total: null,
  },
}

process.stdout.write(JSON.stringify(stubManifest))
process.exit(0)
