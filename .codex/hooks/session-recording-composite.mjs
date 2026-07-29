#!/usr/bin/env node

// Thin project-local entrypoint. All behavior is intentionally passive and
// fail-open; the adapter never makes a permission or continuation decision.
import '../../scripts/session-recording/codex-hook-adapter.mjs'
