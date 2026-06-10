#!/usr/bin/env node

import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { execSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

// Derive repoRoot from script location so it is stable regardless of cwd.
// Hooks invoke this script via `$(git rev-parse --show-toplevel)/scripts/...`
// but shell cwd when the hook fires may be a subdirectory.
const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const agentsDir = path.join(repoRoot, '.codex', 'agents');
const configPath = path.join(repoRoot, '.codex', 'config.toml');
const hooksPath = path.join(repoRoot, '.codex', 'hooks.json');
const rulesPath = path.join(repoRoot, '.codex', 'rules', 'default.rules');
const ledgerPath = path.join(repoRoot, 'artifacts', 'codex', 'subagent-launch-ledger.json');

// CODEX_BIN env override for environments without codex on PATH
const CODEX_BIN = process.env.CODEX_BIN ?? 'codex';

const requiredAgentNames = [
  'codebase-investigator',
  'implementation-worker',
  'issue-author',
  'issue-reviewer',
  'post-merge-cleanup-worker',
  'pr-reviewer-lite',
  'pr-reviewer',
  'review-issue',
  'test-runner',
  'web-researcher',
];

const builtInNames = new Set(['default', 'worker', 'explorer']);
const allowedRuntimeStatuses = new Set([
  'codex_native',
  'claude_skill_required',
  'followup_required',
]);

const expectedProfiles = new Set(['loop-protocol-readonly', 'loop-protocol-rtk']);
// Model mapping (ChatGPT account Codex CLI, v0.136.0+):
//   gpt-5.4-mini medium      = Haiku equivalent  (light read-only / cleanup tasks)
//   gpt-5.4-mini extra-high  = Sonnet equivalent (medium complexity tasks)
//   gpt-5.4 medium           = Opus equivalent   (heavy implementation / review tasks)
const reasoningMap = new Map([
  ['codebase-investigator', { model: 'gpt-5.4-mini', effort: 'medium' }],
  ['implementation-worker', { model: 'gpt-5.4', effort: 'medium' }],
  ['issue-author', { model: 'gpt-5.4-mini', effort: 'xhigh' }],
  ['issue-reviewer', { model: 'gpt-5.4-mini', effort: 'medium' }],
  ['post-merge-cleanup-worker', { model: 'gpt-5.4-mini', effort: 'medium' }],
  ['pr-reviewer-lite', { model: 'gpt-5.4-mini', effort: 'medium' }],
  ['pr-reviewer', { model: 'gpt-5.4', effort: 'medium' }],
  ['review-issue', { model: 'gpt-5.4', effort: 'medium' }],
  ['test-runner', { model: 'gpt-5.4-mini', effort: 'medium' }],
  ['web-researcher', { model: 'gpt-5.4-mini', effort: 'medium' }],
]);

const readOnlyAgents = new Set([
  'codebase-investigator',
  'issue-reviewer',
  'pr-reviewer-lite',
  'pr-reviewer',
  'test-runner',
  'web-researcher',
]);

const writeAgents = new Set([
  'implementation-worker',
  'issue-author',
  'post-merge-cleanup-worker',
  'review-issue',
]);

const supportedPreToolNames = ['Bash', 'apply_patch', 'Edit', 'Write'];
const prohibitedRootActionKinds = new Set([
  'file_edit',
  'test_execution',
  'git_commit',
  'git_push',
  'review_judgment',
  'cleanup_git_mutation',
]);

function readText(filePath) {
  return fs.readFileSync(filePath, 'utf8');
}

function fail(message) {
  process.stderr.write(`${message}\n`);
  process.exit(1);
}

// ---------------------------------------------------------------------------
// Enhanced TOML parser
// Handles: table headers [section], nested table headers [a.b], key = value,
// multi-line strings """, duplicate key detection, unclosed string detection.
// Known limitation: does not implement the full TOML spec (e.g. inline tables,
// arrays of tables [[...]]), date types, or hex/octal integers). Codex real
// loader compatibility smoke (codex --strict-config) requires a model call and
// is deferred to human smoke verification.
// ---------------------------------------------------------------------------
function parseTomlFile(filePath) {
  const text = readText(filePath);
  const root = {};
  // Track defined keys per section object using WeakMap (section identity, not content)
  const sectionKeys = new WeakMap();
  let currentSection = root;
  sectionKeys.set(root, new Set());
  const lines = text.split(/\r?\n/);

  for (let i = 0; i < lines.length; i += 1) {
    const rawLine = lines[i];
    const line = rawLine.trim();

    if (!line || line.startsWith('#')) {
      continue;
    }

    // Table header: [section] or [a.b.c]
    if (line.startsWith('[') && !line.startsWith('[[')) {
      const headerMatch = /^\[([^\]]+)\]$/.exec(line);
      if (headerMatch) {
        const sectionPath = headerMatch[1].trim().split('.');
        currentSection = root;
        for (const part of sectionPath) {
          if (!(part in currentSection)) {
            const newSection = {};
            currentSection[part] = newSection;
            sectionKeys.set(newSection, new Set());
          } else if (typeof currentSection[part] !== 'object' || Array.isArray(currentSection[part])) {
            // Redefining a scalar key as a table is a parse error
            throw new Error(`${filePath}:${i + 1}: duplicate or conflicting key "${part}" in section path "${headerMatch[1]}"`);
          }
          currentSection = currentSection[part];
        }
        continue;
      }
      throw new Error(`${filePath}:${i + 1}: malformed table header: ${line}`);
    }

    // Array of tables [[section]]
    if (line.startsWith('[[')) {
      // We note it but don't deeply parse; skip to avoid false parse errors
      continue;
    }

    // Key = value
    const kvMatch = /^([A-Za-z0-9_.-]+)\s*=\s*(.+)$/.exec(line);
    if (!kvMatch) {
      continue;
    }
    const [, key, rawValue] = kvMatch;

    // Duplicate key detection (within current section, by object identity)
    if (!sectionKeys.has(currentSection)) {
      sectionKeys.set(currentSection, new Set());
    }
    const keysInSection = sectionKeys.get(currentSection);
    if (keysInSection.has(key)) {
      throw new Error(`${filePath}:${i + 1}: duplicate key "${key}"`);
    }
    keysInSection.add(key);

    // Multi-line string: value starts with triple-quote
    if (rawValue === '"""' || rawValue.startsWith('"""')) {
      const inlineRemainder = rawValue.slice(3);
      if (inlineRemainder.endsWith('"""') && inlineRemainder.length > 0) {
        currentSection[key] = inlineRemainder.slice(0, -3);
        continue;
      }
      // Multi-line: collect until closing """
      const collected = inlineRemainder ? [inlineRemainder] : [];
      i += 1;
      let closed = false;
      while (i < lines.length) {
        if (lines[i].trimEnd() === '"""' || lines[i] === '"""') {
          closed = true;
          break;
        }
        collected.push(lines[i]);
        i += 1;
      }
      if (!closed) {
        throw new Error(`${filePath}: unclosed multi-line string for key "${key}" starting at line ${i + 1}`);
      }
      currentSection[key] = collected.join('\n');
      continue;
    }

    // Single-line string
    if (rawValue.startsWith('"')) {
      // Check for unclosed single-line string (must end with unescaped ")
      const closeIdx = rawValue.indexOf('"', 1);
      if (closeIdx === -1) {
        throw new Error(`${filePath}:${i + 1}: unclosed string for key "${key}"`);
      }
      try {
        currentSection[key] = JSON.parse(rawValue);
      } catch {
        throw new Error(`${filePath}:${i + 1}: invalid string value for key "${key}": ${rawValue}`);
      }
      continue;
    }

    // Literal string
    if (rawValue.startsWith("'")) {
      const closeIdx = rawValue.indexOf("'", 1);
      if (closeIdx === -1) {
        throw new Error(`${filePath}:${i + 1}: unclosed literal string for key "${key}"`);
      }
      currentSection[key] = rawValue.slice(1, closeIdx);
      continue;
    }

    // Boolean / integer / float / other scalar
    currentSection[key] = rawValue;
  }
  return root;
}

function getAgentFiles() {
  if (!fs.existsSync(agentsDir)) {
    fail(`missing directory: ${path.relative(repoRoot, agentsDir)}`);
  }
  return fs.readdirSync(agentsDir)
    .filter((entry) => entry.endsWith('.toml'))
    .sort();
}

function extractRuntimeStatus(instructions) {
  const match = instructions.match(/runtime_dependency_status:\s*([a-z_]+)/);
  return match?.[1] ?? null;
}

function assert(condition, message, failures) {
  if (!condition) {
    failures.push(message);
  }
}

// ---------------------------------------------------------------------------
// hooks.json structural validation (JSON.parse-based, not string includes)
// ---------------------------------------------------------------------------
function validateHooksJson(hooksPath, failures) {
  let parsed;
  try {
    parsed = JSON.parse(readText(hooksPath));
  } catch (error) {
    failures.push(`hooks.json: JSON parse error: ${error.message}`);
    return;
  }

  assert(parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed), 'hooks.json: root must be an object', failures);
  assert(Array.isArray(parsed?.hooks?.SubagentStart), 'hooks.json: must have hooks.SubagentStart array', failures);
  assert(Array.isArray(parsed?.hooks?.PreToolUse), 'hooks.json: must have hooks.PreToolUse array', failures);

  const subagentEntries = parsed?.hooks?.SubagentStart ?? [];
  assert(subagentEntries.length === 1, 'hooks.json: SubagentStart must have exactly one entry', failures);
  if (subagentEntries.length === 1) {
    assert(subagentEntries[0]?.matcher === '.*', 'hooks.json: SubagentStart matcher must be .*', failures);
    const subagentHooks = subagentEntries[0]?.hooks ?? [];
    assert(subagentHooks.length === 1, 'hooks.json: SubagentStart must have exactly one command hook', failures);
    assert(
      subagentHooks[0]?.command?.includes('--hook-subagent-start'),
      'hooks.json: SubagentStart command must include --hook-subagent-start',
      failures,
    );
  }

  const preToolEntries = parsed?.hooks?.PreToolUse ?? [];
  const expectedMatchers = new Map([
    ['^Bash$', 'Checking LOOP_PROTOCOL Bash guardrail'],
    ['^(apply_patch|Edit|Write)$', 'Checking LOOP_PROTOCOL patch guardrail'],
  ]);
  for (const [matcher, expectedStatusMessage] of expectedMatchers) {
    const entry = preToolEntries.find((candidate) => candidate?.matcher === matcher);
    assert(Boolean(entry), `hooks.json: missing PreToolUse matcher ${matcher}`, failures);
    if (!entry) {
      continue;
    }
    const hooks = entry?.hooks ?? [];
    // PreToolUse matchers must have exactly 2 hooks (active handler inventory, #783):
    //   1. scripts/check-codex-agents.mjs --hook-pretool (rtk bypass guard / Allowed Paths enforcement)
    //   2. .codex/hooks/session-recording-composite.mjs --event PreToolUse (session recording guard)
    assert(hooks.length >= 2, `hooks.json: matcher ${matcher} must have at least 2 hooks (check-codex-agents + session-recording-composite)`, failures);
    const pretoolHook = hooks.find((h) => h?.command?.includes('--hook-pretool'));
    assert(
      Boolean(pretoolHook),
      `hooks.json: matcher ${matcher} must have a hook command including --hook-pretool`,
      failures,
    );
    assert(
      pretoolHook?.statusMessage === expectedStatusMessage,
      `hooks.json: matcher ${matcher} statusMessage must be ${expectedStatusMessage}`,
      failures,
    );
    // Verify session-recording-composite.mjs --event PreToolUse is an active handler (#783)
    const sessionRecordingHook = hooks.find(
      (h) => h?.command?.includes('session-recording-composite.mjs') && h?.command?.includes('--event PreToolUse'),
    );
    assert(
      Boolean(sessionRecordingHook),
      `hooks.json: matcher ${matcher} must have session-recording-composite.mjs --event PreToolUse as an active handler (Fix 4 #783)`,
      failures,
    );
  }

  const allHookCommands = [];
  for (const eventKey of ['SubagentStart', 'PreToolUse']) {
    const entries = parsed?.hooks?.[eventKey] ?? [];
    for (const entry of entries) {
      const hooks = entry?.hooks ?? [];
      for (const hook of hooks) {
        if (hook?.command) {
          allHookCommands.push(hook.command);
        }
      }
    }
  }

  const joinedCommands = allHookCommands.join('\n');
  assert(
    joinedCommands.includes('scripts/check-codex-agents.mjs'),
    'hooks.json: at least one hook command must route through scripts/check-codex-agents.mjs',
    failures,
  );
  assert(
    joinedCommands.includes('$(git rev-parse --show-toplevel)'),
    'hooks.json: hook command must resolve path from git root via $(git rev-parse --show-toplevel)',
    failures,
  );
  assert(
    joinedCommands.includes('rtk pnpm exec node'),
    'hooks.json: hook command must invoke validator through rtk pnpm exec node',
    failures,
  );
}

// ---------------------------------------------------------------------------
// execpolicy smoke validation using codex execpolicy check
// Fail-closed: if codex binary not found, validator FAIL (not silent skip).
// ---------------------------------------------------------------------------
function validateExecpolicy(failures) {
  // Check codex binary availability
  let codexAvailable = true;
  try {
    execSync(`${CODEX_BIN} --version`, { stdio: 'pipe' });
  } catch {
    codexAvailable = false;
    process.stderr.write(
      `[execpolicy] SKIP: codex binary not found at ${CODEX_BIN}. ` +
      'Set CODEX_ALLOW_NO_CODEX=1 to convert to warning, or set CODEX_BIN to override path.\n',
    );
    if (!process.env.CODEX_ALLOW_NO_CODEX) {
      failures.push(
        'execpolicy: codex binary unavailable (fail-closed). ' +
        'Set CODEX_ALLOW_NO_CODEX=1 to skip this check in environments without codex.',
      );
    }
    return;
  }

  // Representative command checks
  const checks = [
    // read-only: must be allow
    { tokens: ['rtk', 'gh', 'issue', 'view', '1'], expectedDecision: 'allow' },
    { tokens: ['rtk', 'pnpm', 'test'], expectedDecision: 'allow' },
    // mutations: must NOT be allow (prompt or forbidden)
    { tokens: ['rtk', 'gh', 'pr', 'merge', '1'], expectedDecision: 'prompt', expectNotAllow: true },
    { tokens: ['rtk', 'git', 'push'], expectedDecision: 'prompt', expectNotAllow: true },
    { tokens: ['rtk', 'pnpm', 'add', 'lodash'], expectedDecision: 'prompt', expectNotAllow: true },
    // direct bypass: must be forbidden
    { tokens: ['git', 'push'], expectedDecision: 'forbidden' },
    { tokens: ['pnpm', 'test'], expectedDecision: 'forbidden' },
    { tokens: ['gh', 'issue', 'create'], expectedDecision: 'forbidden' },
  ];

  for (const check of checks) {
    const tokenArgs = check.tokens.map((t) => JSON.stringify(t)).join(' ');
    try {
      const output = execSync(
        `${CODEX_BIN} execpolicy check --rules ${JSON.stringify(rulesPath)} ${check.tokens.map((t) => JSON.stringify(t)).join(' ')}`,
        { stdio: ['pipe', 'pipe', 'pipe'] },
      ).toString().trim();
      let result;
      try {
        result = JSON.parse(output);
      } catch {
        failures.push(`execpolicy: command "${check.tokens.join(' ')}": non-JSON output: ${output.slice(0, 200)}`);
        continue;
      }
      const decision = result?.decision;
      if (check.expectNotAllow) {
        // We require NOT allow (prompt or forbidden both acceptable)
        if (decision === 'allow') {
          failures.push(
            `execpolicy: command "${check.tokens.join(' ')}": expected NOT allow (got allow). ` +
            `rtk trust boundary violation: mutation commands must be prompt or forbidden.`,
          );
        }
      } else {
        if (decision !== check.expectedDecision) {
          failures.push(
            `execpolicy: command "${check.tokens.join(' ')}": expected "${check.expectedDecision}" got "${decision}"`,
          );
        }
      }
    } catch (error) {
      failures.push(
        `execpolicy: failed to run check for "${check.tokens.join(' ')}": ${error.message}`,
      );
    }
  }
}

function validateAgents() {
  const failures = [];
  const warnings = [];
  const files = getAgentFiles();
  assert(files.length === requiredAgentNames.length, `expected ${requiredAgentNames.length} agent files, found ${files.length}`, failures);

  const seenNames = new Set();

  // config.toml: structural checks via our enhanced TOML parser
  let configParsed = null;
  try {
    configParsed = parseTomlFile(configPath);
  } catch (error) {
    failures.push(`config.toml: parse error: ${error.message}`);
  }
  const configText = readText(configPath);
  const rulesText = readText(rulesPath);

  assert(configText.includes('default_permissions = "loop-protocol-rtk"'), 'config.toml must keep default_permissions = "loop-protocol-rtk"', failures);
  assert(configText.includes('[permissions.loop-protocol-readonly.filesystem]'), 'config.toml must define permissions.loop-protocol-readonly', failures);
  assert(configText.includes('.codex/hooks.json'), 'config.toml must mention .codex/hooks.json as the documented hook surface', failures);
  assert(!configText.includes('sandbox_mode'), 'config.toml must not use sandbox_mode when permission profiles are active', failures);
  assert(rulesText.includes('fail-closed local guardrail'), 'default.rules must describe hooks/rules as a fail-closed local guardrail', failures);
  assert(rulesText.includes('Known limitation'), 'default.rules must mention Known limitation wording', failures);

  // hooks.json: JSON structural validation
  validateHooksJson(hooksPath, failures);

  // execpolicy smoke
  validateExecpolicy(failures);

  for (const requiredName of requiredAgentNames) {
    const filename = `${requiredName}.toml`;
    assert(files.includes(filename), `missing agent file: ${filename}`, failures);
  }

  for (const file of files) {
    const filePath = path.join(agentsDir, file);
    let parsed;
    try {
      parsed = parseTomlFile(filePath);
    } catch (error) {
      failures.push(`${file}: TOML parse error: ${error.message}`);
      continue;
    }
    const name = parsed.name;
    const instructions = parsed.developer_instructions ?? '';
    const runtimeStatus = extractRuntimeStatus(instructions);
    const expected = reasoningMap.get(name);

    assert(Boolean(name), `${file}: missing name`, failures);
    assert(Boolean(parsed.description), `${file}: missing description`, failures);
    assert(Boolean(parsed.developer_instructions), `${file}: missing developer_instructions`, failures);
    assert(file === `${name}.toml`, `${file}: filename must match name`, failures);
    assert(!builtInNames.has(name), `${file}: custom agent name collides with built-in agent`, failures);
    assert(!seenNames.has(name), `${file}: duplicate agent name`, failures);
    seenNames.add(name);

    assert(Boolean(expected), `${file}: no model/reasoning mapping registered`, failures);
    if (expected) {
      assert(parsed.model === expected.model, `${file}: model must be ${expected.model}`, failures);
      assert(parsed.model_reasoning_effort === expected.effort, `${file}: model_reasoning_effort must be ${expected.effort}`, failures);
    }

    assert(expectedProfiles.has(parsed.default_permissions), `${file}: default_permissions must use the permission profile strategy`, failures);
    assert(!('sandbox_mode' in parsed), `${file}: sandbox_mode must not be mixed with permission profiles`, failures);
    assert(Boolean(runtimeStatus), `${file}: runtime_dependency_status is required`, failures);
    assert(allowedRuntimeStatuses.has(runtimeStatus), `${file}: runtime_dependency_status must be one of ${[...allowedRuntimeStatuses].join(', ')}`, failures);
    assert(instructions.includes('structured output'), `${file}: developer_instructions must require structured output`, failures);
    assert(instructions.includes('context budget'), `${file}: developer_instructions must mention context budget`, failures);
    assert(instructions.includes('progressive disclosure'), `${file}: developer_instructions must mention progressive disclosure`, failures);
    assert(instructions.includes('validator-first'), `${file}: developer_instructions must mention validator-first`, failures);
    assert(instructions.includes('raw transcript'), `${file}: developer_instructions must forbid raw transcript`, failures);
    assert(instructions.includes('raw diff'), `${file}: developer_instructions must forbid raw diff`, failures);
    assert(instructions.includes('raw logs'), `${file}: developer_instructions must forbid raw logs`, failures);
    assert(instructions.includes('fail-closed local guardrail'), `${file}: developer_instructions must use local guardrail wording`, failures);
    assert(instructions.includes('Known limitation'), `${file}: developer_instructions must include Known limitation wording`, failures);

    if (readOnlyAgents.has(name)) {
      assert(parsed.default_permissions === 'loop-protocol-readonly', `${file}: read-only agent must use loop-protocol-readonly`, failures);
    }
    if (writeAgents.has(name)) {
      assert(parsed.default_permissions === 'loop-protocol-rtk', `${file}: write-capable agent must use loop-protocol-rtk`, failures);
    }
  }

  return { failures, warnings };
}

// ---------------------------------------------------------------------------
// Hook input parsing
// ---------------------------------------------------------------------------
function parseHookInput() {
  const input = fs.readFileSync(0, 'utf8').trim();
  if (!input) {
    return {};
  }
  try {
    return JSON.parse(input);
  } catch (error) {
    return { parse_error: String(error), raw: input };
  }
}

function ensureLedgerDirectory() {
  fs.mkdirSync(path.dirname(ledgerPath), { recursive: true });
}

function defaultCoverageScope() {
  return {
    subagent_start_event_recorded: true,
    supported_pretooluse_paths: supportedPreToolNames,
    unsupported_paths_fail_closed: true,
    scope_note: 'This ledger records event-derived SubagentStart launches and supported PreToolUse paths only.',
  };
}

function loadLedger() {
  if (!fs.existsSync(ledgerPath)) {
    return {
      ledger_schema: 'SUBAGENT_LAUNCH_LEDGER_V1',
      generated_by: 'codex_hook_pipeline',
      generated_at: new Date().toISOString(),
      ledger_path: path.relative(repoRoot, ledgerPath),
      codex_binary_status: 'available',
      coverage_scope: defaultCoverageScope(),
      launches: [],
      root_thread_actions: [],
    };
  }
  try {
    return JSON.parse(readText(ledgerPath));
  } catch {
    return {
      ledger_schema: 'SUBAGENT_LAUNCH_LEDGER_V1',
      generated_by: 'codex_hook_pipeline',
      generated_at: new Date().toISOString(),
      ledger_path: path.relative(repoRoot, ledgerPath),
      codex_binary_status: 'available',
      coverage_scope: defaultCoverageScope(),
      launches: [],
      root_thread_actions: [],
    };
  }
}

function saveLedger(ledger) {
  ensureLedgerDirectory();
  ledger.generated_at = new Date().toISOString();
  ledger.ledger_path = path.relative(repoRoot, ledgerPath);
  fs.writeFileSync(ledgerPath, `${JSON.stringify(ledger, null, 2)}\n`, 'utf8');
}

function getAgentRuntime(agentName) {
  const filePath = path.join(agentsDir, `${agentName}.toml`);
  if (!fs.existsSync(filePath)) {
    return null;
  }
  const parsed = parseTomlFile(filePath);
  return {
    model: parsed.model,
    reasoning_effort: parsed.model_reasoning_effort,
    default_permissions: parsed.default_permissions,
  };
}

function buildEventFingerprint(payload, fallback) {
  return [
    payload?.session_id,
    payload?.agent_id,
    payload?.tool_use_id,
    payload?.tool_name,
    payload?.cwd,
    fallback,
  ].filter(Boolean).join(':');
}

function classifyRootThreadAction(toolName, command) {
  if (toolName === 'Bash') {
    if (/\b(uv|pytest|pnpm\s+(test|lint|build|typecheck))\b/.test(command)) {
      return 'test_execution';
    }
    if (/\brtk\s+gh\s+pr\s+review\b/.test(command)) {
      return 'review_judgment';
    }
    if (/\brtk\s+git\s+commit\b/.test(command)) {
      return 'git_commit';
    }
    if (/\brtk\s+git\s+push\b/.test(command)) {
      return 'git_push';
    }
    if (/\brtk\s+git\s+branch\s+-D\b|\brtk\s+git\s+worktree\s+remove\b/.test(command)) {
      return 'cleanup_git_mutation';
    }
    return 'bash_observed';
  }
  if (toolName === 'apply_patch' || toolName === 'Edit' || toolName === 'Write') {
    return 'file_edit';
  }
  return 'unsupported_tool_path';
}

function appendLaunchEvidence(payload) {
  const agentType = payload.agent_type ?? payload.subagent_type ?? 'unknown-agent';
  const runtime = getAgentRuntime(agentType);
  if (!runtime) {
    return;
  }
  const fingerprint = buildEventFingerprint(payload, `SubagentStart:${agentType}`);
  const ledger = loadLedger();
  const alreadyPresent = ledger.launches.some((launch) => launch.event_fingerprint === fingerprint);
  if (!alreadyPresent) {
    ledger.launches.push({
      agent_name: agentType,
      event_type: 'SubagentStart',
      evidence_source: 'event_derived',
      event_fingerprint: fingerprint,
      runtime,
    });
  }
  saveLedger(ledger);
}

function appendPreToolEvidence(payload, toolName, command) {
  const ledger = loadLedger();
  const observedCommand = command || payload?.tool_input?.file_path || payload?.tool_input?.command || '';
  const action = {
    kind: classifyRootThreadAction(toolName, command),
    command: observedCommand,
    tool_name: toolName,
    coverage_source: 'supported_pretooluse_path',
  };
  const duplicate = ledger.root_thread_actions.some(
    (entry) => entry.tool_name === action.tool_name && entry.command === action.command,
  );
  if (!duplicate) {
    ledger.root_thread_actions.push(action);
  }
  saveLedger(ledger);
}

function hookDeny(reason) {
  process.stdout.write(JSON.stringify({
    hookSpecificOutput: {
      hookEventName: 'PreToolUse',
      permissionDecision: 'deny',
      permissionDecisionReason: reason,
    },
  }));
}

function hookAdditionalContext(hookEventName, additionalContext) {
  process.stdout.write(JSON.stringify({
    hookSpecificOutput: {
      hookEventName,
      additionalContext,
    },
  }));
}

function normalizeCommand(value) {
  return typeof value === 'string' ? value.trim() : '';
}

function isMutatingGit(command) {
  return /^git\s+(add|commit|push|switch|checkout|worktree|stash|merge|rebase|cherry-pick|reset|restore|pull|fetch|remote|tag|config|submodule|rm|mv|clean|apply|am)\b/.test(command);
}

function isDirectBypass(command) {
  return (
    /^pnpm\b/.test(command) ||
    /^gh\b/.test(command) ||
    isMutatingGit(command) ||
    /^git\s+add\s+(\.|-A)(\s|$)/.test(command)
  );
}

function denyForBash(command) {
  if (!command) {
    return null;
  }
  // rtk-prefixed commands pass through (read-only investigation allowed)
  if (/^rtk\b/.test(command)) {
    return null; // readonly_investigation_allowed: rtk commands pass through
  }
  if (isDirectBypass(command)) {
    // direct_bypass_requires_rtk: non-rtk direct invocation of pnpm / gh / mutating git
    return 'direct_bypass_requires_rtk: Use repo-documented rtk wrappers instead of direct pnpm / gh / mutating git commands.';
  }
  return null;
}

// ---------------------------------------------------------------------------
// Allowed Paths enforcement (Fix B)
// Behavior:
//   - If CODEX_ALLOWED_PATHS is set: enforce strictly — writes outside the set
//     are denied (fail-closed).
//   - If CODEX_ALLOWED_PATHS is NOT set (default): deny all writes except
//     assets/ and LICENSES/ protection (fail-closed by design).
//     Set CODEX_LEGACY_ALLOW_WRITES=1 to restore the old allow-by-default
//     behavior for callers that have not yet adopted CODEX_ALLOWED_PATHS.
// Rationale: fail-closed is the correct default for a guardrail. Callers
// must declare allowed paths per Issue contract; the legacy bypass is an
// explicit opt-out, not the default.
//
// KNOWN LIMITATION: path canonicalization uses path.resolve without realpath,
// so symlinks that escape the repo root are not caught.
// ---------------------------------------------------------------------------
function parseAllowedPaths() {
  const raw = process.env.CODEX_ALLOWED_PATHS;
  if (!raw || !raw.trim()) {
    return null; // not set: use fail-closed default (or legacy mode if opted in)
  }
  // Accept newline-separated paths (colon is avoided: conflicts with Windows drive letters)
  return raw.split(/\n+/).map((p) => p.trim()).filter(Boolean);
}

function resolveInsideRepo(inputPath) {
  if (!inputPath || inputPath.includes('\0')) return null;
  const resolved = path.resolve(repoRoot, inputPath);
  // Reject paths that escape the repo root
  if (resolved !== repoRoot && !resolved.startsWith(repoRoot + path.sep)) return null;
  return resolved;
}

function isPathAllowed(filePath, allowedPaths) {
  const candidate = resolveInsideRepo(filePath);
  if (!candidate) return false;
  return allowedPaths.some((allowed) => {
    const resolvedAllowed = resolveInsideRepo(allowed);
    return resolvedAllowed && (candidate === resolvedAllowed || candidate.startsWith(resolvedAllowed + path.sep));
  });
}

function isProtectedLegacy(filePath) {
  const candidate = resolveInsideRepo(filePath);
  if (!candidate) return true; // null path (traversal/NUL) → deny
  const assetsDir = path.resolve(repoRoot, 'assets');
  const licensesDir = path.resolve(repoRoot, 'LICENSES');
  return (
    candidate === assetsDir || candidate.startsWith(assetsDir + path.sep) ||
    candidate === licensesDir || candidate.startsWith(licensesDir + path.sep)
  );
}

function extractPatchTouchedPaths(command) {
  return [...command.matchAll(/\*\*\* (?:Add|Delete|Update) File: (.+)$/gm)].map((match) => match[1].trim());
}

function denyForWriteTool(toolName, toolInput, allowedPaths) {
  const legacyMode = process.env.CODEX_LEGACY_ALLOW_WRITES === '1';

  // apply_patch: extract touched paths from patch content
  if (toolName === 'apply_patch') {
    const command = normalizeCommand(toolInput?.command);
    const touchedPaths = extractPatchTouchedPaths(command);
    if (allowedPaths !== null) {
      // Strict mode: check against Allowed Paths
      for (const filePath of touchedPaths) {
        if (!isPathAllowed(filePath, allowedPaths)) {
          // AC5: allowed_paths_violation — path is outside declared Allowed Paths
          return `allowed_paths_violation: "${filePath}" is outside the declared Allowed Paths set. (CODEX_ALLOWED_PATHS enforcement)`;
        }
      }
    } else if (legacyMode) {
      // Legacy opt-in: only block assets/LICENSES
      if (touchedPaths.some((fp) => isProtectedLegacy(fp))) {
        return 'assets/ and LICENSES/ are human-managed and blocked by the local guardrail.';
      }
    } else {
      // Fail-closed default: CODEX_ALLOWED_PATHS must be declared
      if (touchedPaths.length > 0) {
        // AC5: allowed_paths_missing — CODEX_ALLOWED_PATHS not declared
        return 'allowed_paths_missing: CODEX_ALLOWED_PATHS is not set. Declare allowed paths per Issue contract or set CODEX_LEGACY_ALLOW_WRITES=1 to opt out.';
      }
    }
    return null;
  }

  // Edit and Write: check file_path
  if (toolName === 'Edit' || toolName === 'Write') {
    const filePath = toolInput?.file_path ?? '';
    if (!filePath) {
      return null; // no path to check
    }
    if (allowedPaths !== null) {
      // Strict mode
      if (!isPathAllowed(filePath, allowedPaths)) {
        // AC5: allowed_paths_violation — path is outside declared Allowed Paths
        return `allowed_paths_violation: "${filePath}" is outside the declared Allowed Paths set. (CODEX_ALLOWED_PATHS enforcement)`;
      }
    } else if (legacyMode) {
      // Legacy opt-in: only block assets/LICENSES
      if (isProtectedLegacy(filePath)) {
        return 'assets/ and LICENSES/ are human-managed and blocked by the local guardrail.';
      }
    } else {
      // Fail-closed default: CODEX_ALLOWED_PATHS must be declared
      // AC5: allowed_paths_missing — CODEX_ALLOWED_PATHS not declared
      return 'allowed_paths_missing: CODEX_ALLOWED_PATHS is not set. Declare allowed paths per Issue contract or set CODEX_LEGACY_ALLOW_WRITES=1 to opt out.';
    }
    return null;
  }

  return null;
}

function runPreToolHook() {
  const payload = parseHookInput();
  const toolName = payload.tool_name;
  const toolInput = payload?.tool_input ?? {};
  const command = normalizeCommand(toolInput?.command);
  const allowedPaths = parseAllowedPaths();
  let reason = null;

  if (supportedPreToolNames.includes(toolName)) {
    appendPreToolEvidence(payload, toolName, command);
  }

  if (toolName === 'Bash') {
    reason = denyForBash(command);
  } else if (toolName === 'apply_patch' || toolName === 'Edit' || toolName === 'Write') {
    reason = denyForWriteTool(toolName, toolInput, allowedPaths);
  }

  if (reason) {
    hookDeny(reason);
  }
}

function runSubagentStartHook() {
  const payload = parseHookInput();
  const agentType = payload.agent_type ?? 'unknown-agent';
  appendLaunchEvidence(payload);
  const isReadOnly = readOnlyAgents.has(agentType);
  const additionalContext = [
    `Agent ${agentType}: keep main-thread context budget low and return structured output only.`,
    'Use progressive disclosure and validator-first checks before long prose.',
    'Use rtk only for subcommands listed in rtk --help; if a read-only helper has no rtk wrapper, use direct sed, rg, find, or other direct read-only commands instead of inventing rtk wrappers.',
    'Do not surface raw transcript, raw diff, or raw logs to the parent thread.',
    isReadOnly
      ? 'This agent is configured for a read-only permission profile; do not attempt repo edits or write primitives.'
      : 'This agent is configured for the write-capable loop-protocol-rtk profile; stay inside the repo workflow and declared Allowed Paths.',
    'Known limitation: hooks and permission profiles are a fail-closed local guardrail, not a security boundary.',
  ].join(' ');
  hookAdditionalContext('SubagentStart', additionalContext);
}

// ---------------------------------------------------------------------------
// --self-test: synthetic hook payload tests for Allowed Paths enforcement
// ---------------------------------------------------------------------------
function runSelfTest() {
  const selfTestFailures = [];

  function selfAssert(condition, label) {
    if (condition) {
      process.stdout.write(`  PASS ${label}\n`);
    } else {
      process.stdout.write(`  FAIL ${label}\n`);
      selfTestFailures.push(label);
    }
  }

  process.stdout.write('=== self-test: TOML parser ===\n');
  // Valid TOML round-trip
  try {
    const parsed = parseTomlFile(configPath);
    selfAssert(typeof parsed === 'object', 'config.toml: parses without error');
    selfAssert(
      typeof parsed.default_permissions === 'string',
      'config.toml: default_permissions is a string',
    );
  } catch (e) {
    selfAssert(false, `config.toml: should parse cleanly (got: ${e.message})`);
  }

  // Duplicate key detection (synthetic)
  {
    const syntheticToml = [
      'name = "test"',
      'name = "duplicate"',
    ].join('\n');
    // Write a temp file, parse, check for error
    const tmpPath = path.join(repoRoot, '.codex', '.self-test-tmp.toml');
    fs.writeFileSync(tmpPath, syntheticToml, 'utf8');
    let threw = false;
    try {
      parseTomlFile(tmpPath);
    } catch {
      threw = true;
    }
    fs.unlinkSync(tmpPath);
    selfAssert(threw, 'TOML parser: detects duplicate key');
  }

  process.stdout.write('\n=== self-test: Allowed Paths enforcement (Edit/Write) ===\n');

  // Test: CODEX_ALLOWED_PATHS not set, no legacy opt-in — fail-closed: any write is denied
  {
    delete process.env.CODEX_ALLOWED_PATHS;
    delete process.env.CODEX_LEGACY_ALLOW_WRITES;
    const result = denyForWriteTool('Edit', { file_path: 'src/main.ts' }, null);
    selfAssert(result !== null, 'Fail-closed default: src/main.ts edit is denied when CODEX_ALLOWED_PATHS not set');
  }

  // Test: CODEX_ALLOWED_PATHS not set, CODEX_LEGACY_ALLOW_WRITES=1 — assets/ is denied
  {
    delete process.env.CODEX_ALLOWED_PATHS;
    process.env.CODEX_LEGACY_ALLOW_WRITES = '1';
    const result = denyForWriteTool('Edit', { file_path: 'assets/sprite.png' }, null);
    delete process.env.CODEX_LEGACY_ALLOW_WRITES;
    selfAssert(result !== null, 'Legacy opt-in: assets/ edit is denied');
  }

  // Test: CODEX_ALLOWED_PATHS not set, CODEX_LEGACY_ALLOW_WRITES=1 — normal path is allowed
  {
    delete process.env.CODEX_ALLOWED_PATHS;
    process.env.CODEX_LEGACY_ALLOW_WRITES = '1';
    const result = denyForWriteTool('Edit', { file_path: 'src/main.ts' }, null);
    delete process.env.CODEX_LEGACY_ALLOW_WRITES;
    selfAssert(result === null, 'Legacy opt-in: src/main.ts edit is allowed');
  }

  // Test: CODEX_ALLOWED_PATHS set — path inside set is allowed
  {
    const allowed = ['.codex/agents', 'scripts'];
    const result = denyForWriteTool('Write', { file_path: '.codex/agents/foo.toml' }, allowed);
    selfAssert(result === null, 'Strict mode: .codex/agents/foo.toml is allowed');
  }

  // Test: CODEX_ALLOWED_PATHS set — path outside set is denied
  {
    const allowed = ['.codex/agents', 'scripts'];
    const result = denyForWriteTool('Write', { file_path: 'src/main.ts' }, allowed);
    selfAssert(result !== null, 'Strict mode: src/main.ts is denied (outside Allowed Paths)');
  }

  // Test: assets/ is denied even with unrelated Allowed Paths set
  {
    const allowed = ['.codex/agents'];
    const result = denyForWriteTool('Edit', { file_path: 'assets/sprite.png' }, allowed);
    selfAssert(result !== null, 'Strict mode: assets/sprite.png is denied');
  }

  // Test: path traversal escape — denied in strict mode
  {
    const allowed = ['scripts'];
    const result = denyForWriteTool('Write', { file_path: 'scripts/../src/main.ts' }, allowed);
    selfAssert(result !== null, 'Strict mode: path traversal escape scripts/../src/main.ts is denied');
  }

  // Test: path traversal escape — denied in fail-closed default
  {
    delete process.env.CODEX_ALLOWED_PATHS;
    delete process.env.CODEX_LEGACY_ALLOW_WRITES;
    const result = denyForWriteTool('Write', { file_path: 'scripts/../src/main.ts' }, null);
    selfAssert(result !== null, 'Fail-closed default: path traversal escape is denied');
  }

  // Test: NUL byte in path — denied
  {
    const allowed = ['scripts'];
    const result = denyForWriteTool('Write', { file_path: 'scripts/foo\0bar.mjs' }, allowed);
    selfAssert(result !== null, 'Strict mode: NUL byte in path is denied');
  }

  process.stdout.write('\n=== self-test: Allowed Paths enforcement (apply_patch) ===\n');

  // Test: apply_patch with assets path — fail-closed default deny
  {
    delete process.env.CODEX_ALLOWED_PATHS;
    delete process.env.CODEX_LEGACY_ALLOW_WRITES;
    const patchCmd = '*** Update File: assets/sprite.png\n--- a\n+++ b\n';
    const result = denyForWriteTool('apply_patch', { command: patchCmd }, null);
    selfAssert(result !== null, 'Fail-closed default: apply_patch to assets/ is denied');
  }

  // Test: apply_patch with assets path — legacy opt-in deny
  {
    process.env.CODEX_LEGACY_ALLOW_WRITES = '1';
    const patchCmd = '*** Update File: assets/sprite.png\n--- a\n+++ b\n';
    const result = denyForWriteTool('apply_patch', { command: patchCmd }, null);
    delete process.env.CODEX_LEGACY_ALLOW_WRITES;
    selfAssert(result !== null, 'Legacy opt-in: apply_patch to assets/ is denied');
  }

  // Test: apply_patch with allowed path — strict mode allow
  {
    const patchCmd = '*** Update File: scripts/check-codex-agents.mjs\n--- a\n+++ b\n';
    const result = denyForWriteTool('apply_patch', { command: patchCmd }, ['scripts']);
    selfAssert(result === null, 'Strict mode: apply_patch to scripts/ is allowed');
  }

  // Test: apply_patch with disallowed path — strict mode deny
  {
    const patchCmd = '*** Update File: src/main.ts\n--- a\n+++ b\n';
    const result = denyForWriteTool('apply_patch', { command: patchCmd }, ['.codex/agents', 'scripts']);
    selfAssert(result !== null, 'Strict mode: apply_patch to src/main.ts is denied');
  }

  // Test: apply_patch path traversal — strict mode deny
  {
    const patchCmd = '*** Update File: scripts/../src/main.ts\n--- a\n+++ b\n';
    const result = denyForWriteTool('apply_patch', { command: patchCmd }, ['scripts']);
    selfAssert(result !== null, 'Strict mode: apply_patch path traversal is denied');
  }

  process.stdout.write('\n=== self-test: Bash hook ===\n');

  // Test: rtk gh pr merge is NOT denied by bash hook (rtk prefix passes; policy enforced by execpolicy rules)
  {
    const result = denyForBash('rtk gh pr merge 1');
    selfAssert(result === null, 'Bash hook: rtk gh pr merge is not blocked (rtk prefix passes bash check)');
  }

  // Test: direct gh is denied
  {
    const result = denyForBash('gh pr merge 1');
    selfAssert(result !== null, 'Bash hook: direct gh pr merge is denied');
  }

  // Test: direct pnpm is denied
  {
    const result = denyForBash('pnpm test');
    selfAssert(result !== null, 'Bash hook: direct pnpm test is denied');
  }

  // Test: git push is denied
  {
    const result = denyForBash('git push');
    selfAssert(result !== null, 'Bash hook: git push is denied');
  }

  // Test: direct gh is denied with direct_bypass_requires_rtk reason
  {
    const result = denyForBash('gh pr merge 1');
    selfAssert(result !== null && result.includes('direct_bypass_requires_rtk'), 'Bash hook: direct gh denied with direct_bypass_requires_rtk reason');
  }

  // Test: Edit denied with allowed_paths_missing when CODEX_ALLOWED_PATHS not set
  {
    delete process.env.CODEX_ALLOWED_PATHS;
    delete process.env.CODEX_LEGACY_ALLOW_WRITES;
    const result = denyForWriteTool('Edit', { file_path: 'src/main.ts' }, null);
    selfAssert(result !== null && result.includes('allowed_paths_missing'), 'Write tool: denied with allowed_paths_missing when no CODEX_ALLOWED_PATHS');
  }

  // Test: Write denied with allowed_paths_violation when path is outside Allowed Paths
  {
    const allowed = ['scripts'];
    const result = denyForWriteTool('Write', { file_path: 'src/main.ts' }, allowed);
    selfAssert(result !== null && result.includes('allowed_paths_violation'), 'Write tool: denied with allowed_paths_violation when path outside allowed set');
  }

  process.stdout.write('\n');
  if (selfTestFailures.length > 0) {
    for (const f of selfTestFailures) {
      process.stderr.write(`SELF-TEST FAIL: ${f}\n`);
    }
    process.exit(1);
  }

  process.stdout.write(`ok self-test: ${selfTestFailures.length === 0 ? 'all assertions passed' : 'FAILED'}\n`);
}

function main() {
  const arg = process.argv[2];
  if (arg === '--hook-pretool') {
    runPreToolHook();
    return;
  }
  if (arg === '--hook-subagent-start') {
    runSubagentStartHook();
    return;
  }
  if (arg === '--self-test') {
    runSelfTest();
    return;
  }

  const { failures } = validateAgents();
  if (failures.length > 0) {
    for (const message of failures) {
      process.stderr.write(`FAIL ${message}\n`);
    }
    process.exit(1);
  }

  process.stdout.write(`ok ${requiredAgentNames.length} agents validated\n`);
}

main();
