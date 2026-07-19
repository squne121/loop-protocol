#!/usr/bin/env node

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import process from 'node:process';
import { execFileSync, execSync, spawnSync } from 'node:child_process';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';

// Derive repoRoot from script location so it is stable regardless of cwd.
// Hooks invoke this script via `$(git rev-parse --show-toplevel)/scripts/...`
// but shell cwd when the hook fires may be a subdirectory.
// REPO_ROOT_OVERRIDE allows tests to point the validator at a temporary repo fixture.
const repoRoot = process.env.REPO_ROOT_OVERRIDE
  ? path.resolve(process.env.REPO_ROOT_OVERRIDE)
  : path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const agentsDir = path.join(repoRoot, '.codex', 'agents');
const configPath = path.join(repoRoot, '.codex', 'config.toml');
const hooksPath = path.join(repoRoot, '.codex', 'hooks.json');
const rulesPath = path.join(repoRoot, '.codex', 'rules', 'default.rules');
const ledgerPath = path.join(repoRoot, 'artifacts', 'codex', 'subagent-launch-ledger.json');
// Issue #1611 (contract revision, P1-2): PROTECTED_PATHS_POLICY_V1 JSON SSOT.
// `isProtectedPath()` below reads THIS file directly (not a hand-mirrored
// hardcoded list), so this Node consumer can never silently drift from the
// Python consumer (`scripts/agent-guards/protected_paths_policy.py`).
const protectedPathsPolicyPath = path.join(repoRoot, 'scripts', 'agent-guards', 'protected_paths_policy.v1.json');
const ledgerWriterSource = path.join(repoRoot, 'scripts', 'subagent-launch-ledger-writer.c');
// Issue #1502: the compiled writer binary is built fresh, per invocation,
// into a private `fs.mkdtempSync` directory *outside* the repo snapshot
// (under the OS temp directory), never under a repo-tracked-or-untracked
// path such as `tmp/`. This is deliberate: a per-invocation repo-local build
// artifact (the pre-#1502 `tmp/subagent-launch-ledger-writer*` scheme) made
// every cold/warm hook invocation look like a self-write outside the
// executor's allowed_write_roots, which is exactly the anchor-bound
// preflight false positive this Issue closes.
// Repo-local `tmp/subagent-launch-ledger-writer*` must never be reintroduced
// as a race-tolerant exception (Out of Scope / Stop Condition).
//
// Issue #1502 REQUEST_CHANGES (Blocker 1): a *shared*, content-addressed,
// predictable-path warm cache (`<tmpdir>/loop-protocol-subagent-ledger-
// writer-cache/<sha256>-ledger-writer`) was previously reused across
// invocations after only checking "not a symlink, owner-executable" on the
// cached file itself. Any co-uid process that could write into that shared
// cache directory first could plant an executable there ahead of the real
// compile (TOCTOU / cache poisoning), and `TMPDIR` was inherited unchanged
// into the child environment, so `TMPDIR=<repo>/tmp` could silently move the
// "outside the repo" cache back inside it. The shared warm cache is
// abolished: every invocation validates that `os.tmpdir()` is genuinely
// outside the repo, creates a brand-new unique directory via
// `fs.mkdtempSync`, compiles a *private* copy of the exact `sourceBytes`
// just read (never reopening `ledgerWriterSource` by pathname again), runs
// that private binary, and deletes the whole private directory afterward.
const ledgerWriterPrivateDirPrefix = 'loop-protocol-ledger-writer-';
const sourceRepoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const fixtureRuntimeContractPath = path.join(repoRoot, 'tests', 'fixtures', 'codex-agent-config', 'expected-runtime-contract.json');
const runtimeContractPath = fs.existsSync(fixtureRuntimeContractPath)
  ? fixtureRuntimeContractPath
  : path.join(sourceRepoRoot, 'tests', 'fixtures', 'codex-agent-config', 'expected-runtime-contract.json');

// CODEX_BIN env override for environments without codex on PATH
const CODEX_BIN = process.env.CODEX_BIN ?? 'codex';

// Shadow log path (repo root, git-untracked)

const requiredAgentNames = [
  'codebase-investigator',
  'implementation-worker',
  'issue-author',
  'issue-reviewer',
  'post-merge-cleanup-worker',
  'pr-reviewer-lite',
  'pr-reviewer',
  'review-issue',
  'spark-deep',
  'spark-skim',
  'spark-worker',
  'test-runner',
  'web-researcher',
];

const builtInNames = new Set(['default', 'worker', 'explorer']);
const allowedRuntimeStatuses = new Set([
  'codex_native',
  'codex_skill_required',
  'followup_required',
]);

const expectedProfiles = new Set(['loop-protocol-readonly', 'loop-protocol-rtk']);
function loadReasoningMap() {
  const contract = JSON.parse(fs.readFileSync(runtimeContractPath, 'utf8'));
  if (!contract || typeof contract !== 'object' || !contract.required_agents || typeof contract.required_agents !== 'object') {
    throw new Error(`runtime contract is malformed: ${runtimeContractPath}`);
  }
  const entries = Object.entries(contract.required_agents);
  if (entries.length !== 13) {
    throw new Error(`runtime contract must declare exactly 13 agents: ${runtimeContractPath}`);
  }
  for (const [name, expected] of entries) {
    for (const field of ['path', 'model', 'model_reasoning_effort', 'default_permissions']) {
      if (typeof expected[field] !== 'string' || !expected[field]) {
        throw new Error(`runtime contract ${name}.${field} must be a non-empty string`);
      }
    }
    const isSpark = name.startsWith('spark-');
    if (isSpark && expected.protected_lane !== true) {
      throw new Error(`runtime contract ${name}.protected_lane must be true`);
    }
    if (!isSpark && Object.hasOwn(expected, 'protected_lane')) {
      throw new Error(`runtime contract ${name}.protected_lane is reserved for Spark agents`);
    }
  }
  return new Map(entries.map(([name, expected]) => [name, {
    model: expected.model,
    effort: expected.model_reasoning_effort,
  }]));
}

const reasoningMap = loadReasoningMap();

const readOnlyAgents = new Set([
  'codebase-investigator',
  'issue-reviewer',
  'pr-reviewer-lite',
  'pr-reviewer',
  'spark-deep',
  'spark-skim',
  'test-runner',
  'web-researcher',
]);

const writeAgents = new Set([
  'implementation-worker',
  'issue-author',
  'post-merge-cleanup-worker',
  'review-issue',
  'spark-worker',
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
const requiredDerivedMarker = 'derived/non-canonical';
const requiredImperative = 'Before executing this skill, read the canonical body at';
const maxBridgeBodyLines = 3;

function readText(filePath) {
  return fs.readFileSync(filePath, 'utf8');
}

function readOptionalText(filePath) {
  return fs.existsSync(filePath) ? readText(filePath) : '';
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

function extractSkillSurfacePaths(instructions) {
  const match = instructions.match(/repo_local_skill_surface:\s*(.+)/);
  if (!match) {
    return [];
  }
  return match[1]
    .split(/[|,]/)
    .map((entry) => entry.trim())
    .filter(Boolean);
}

function routeTokensToSkillSurfaces(route) {
  if (!route || route === 'none') {
    return [];
  }
  return route.split('|').filter(Boolean).map((token) => `.agents/skills/${token}/SKILL.md`);
}

function extractRuntimeFollowupRoute(instructions) {
  const match = instructions.match(/runtime_followup_route:\s*([a-zA-Z0-9._|-]+)/);
  return match?.[1] ?? null;
}

function extractCanonicalBodyTarget(skillSurfacePath) {
  const body = readText(skillSurfacePath);
  const match = body.match(/`([^`]*\.claude\/skills\/[^`]+\/SKILL\.md)`/);
  return match?.[1] ?? null;
}

function expectedCanonicalTargetForSurface(skillSurfacePath) {
  return `../../../.claude/skills/${path.basename(path.dirname(skillSurfacePath))}/SKILL.md`;
}

function extractBridgeBodyLines(body) {
  const remainder = body.split('\n---\n').slice(-1)[0];
  return remainder
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith('# '));
}

function validateBridgeSurface(skillSurfacePath, failures) {
  const body = readText(skillSurfacePath);
  const relPath = path.relative(repoRoot, skillSurfacePath);
  const bodyLines = extractBridgeBodyLines(body);

  assert(body.includes(requiredDerivedMarker), `${relPath}: derived/non-canonical marker required`, failures);
  assert(body.includes(requiredImperative), `${relPath}: exact imperative required`, failures);

  const canonicalTarget = extractCanonicalBodyTarget(skillSurfacePath);
  const expectedTarget = expectedCanonicalTargetForSurface(skillSurfacePath);
  if (!canonicalTarget) {
    failures.push(`${relPath}: wrong skill target - canonical target missing`);
  } else if (canonicalTarget !== expectedTarget) {
    failures.push(`${relPath}: wrong skill target - expected ${JSON.stringify(expectedTarget)} got ${JSON.stringify(canonicalTarget)}`);
  } else {
    const canonicalTargetPath = path.resolve(path.dirname(skillSurfacePath), canonicalTarget);
    assert(fs.existsSync(canonicalTargetPath), `${relPath}: canonical skill body target missing for ${canonicalTarget}`, failures);
  }

  assert(bodyLines.length <= maxBridgeBodyLines, `${relPath}: required thin wrapper - body bloat detected`, failures);
  assert(
    !['```', '## ', '### ', '\n- ', '\n* '].some((token) => body.includes(token)),
    `${relPath}: required thin wrapper - stale procedure body detected`,
    failures,
  );
}

function validateDuplicateCanonicalTargets(skillSurfacePaths, failures) {
  const seenTargets = new Map();
  for (const skillSurfacePath of skillSurfacePaths) {
    const canonicalTarget = extractCanonicalBodyTarget(skillSurfacePath);
    if (!canonicalTarget) {
      continue;
    }
    if (seenTargets.has(canonicalTarget)) {
      failures.push(
        `duplicate canonical target: ${canonicalTarget} used by ${path.relative(repoRoot, seenTargets.get(canonicalTarget))} and ${path.relative(repoRoot, skillSurfacePath)}`,
      );
      continue;
    }
    seenTargets.set(canonicalTarget, skillSurfacePath);
  }
}

function assert(condition, message, failures) {
  if (!condition) {
    failures.push(message);
  }
}

const compositeBase =
  'rtk pnpm exec node "$(git rev-parse --show-toplevel)/.codex/hooks/session-recording-composite.mjs"';
const checkCodexAgentsBase =
  'rtk pnpm exec node "$(git rev-parse --show-toplevel)/scripts/check-codex-agents.mjs"';

const expectedCommandHookKeys = ['command', 'statusMessage', 'timeout', 'type'];

function assertExactCommandHook(scope, hook, expected, failures) {
  const actualKeys = hook && typeof hook === 'object' && !Array.isArray(hook) ? Object.keys(hook).sort() : [];
  assert(
    JSON.stringify(actualKeys) === JSON.stringify(expectedCommandHookKeys),
    `${scope}: hook keys must be exactly ${JSON.stringify(expectedCommandHookKeys)}, got ${JSON.stringify(actualKeys)}`,
    failures,
  );
  assert(hook?.type === 'command', `${scope}: type must be command`, failures);
  assert(hook?.command === expected.command, `${scope}: command must exactly match expected handler`, failures);
  assert(hook?.timeout === expected.timeout, `${scope}: timeout must be ${expected.timeout}`, failures);
  assert(
    hook?.statusMessage === expected.statusMessage,
    `${scope}: statusMessage must be ${expected.statusMessage}`,
    failures,
  );
}

function assertExactHookEntry(eventName, entry, expectedMatcher, expectedHooks, failures) {
  assert(Boolean(entry), `hooks.json: missing ${eventName} matcher ${expectedMatcher}`, failures);
  if (!entry) {
    return;
  }
  assert(entry?.matcher === expectedMatcher, `hooks.json: ${eventName} matcher must be ${expectedMatcher}`, failures);
  const hooks = entry?.hooks ?? [];
  assert(Array.isArray(hooks), `hooks.json: ${eventName} ${expectedMatcher} hooks must be an array`, failures);
  if (!Array.isArray(hooks)) {
    return;
  }
  assert(
    hooks.length === expectedHooks.length,
    `hooks.json: ${eventName} ${expectedMatcher} must have exactly ${expectedHooks.length} hooks`,
    failures,
  );
  if (hooks.length !== expectedHooks.length) {
    return;
  }
  for (const [index, expected] of expectedHooks.entries()) {
    assertExactCommandHook(`hooks.json: ${eventName} ${expectedMatcher} hook ${index}`, hooks[index], expected, failures);
  }
}

const expectedPreToolUseEntries = new Map([
  [
    '^Bash$',
    [
      {
        command: 'bash "$(git rev-parse --show-toplevel)/.codex/hooks/local_main_branch_guard.sh"',
        timeout: 10,
        statusMessage: 'Checking local root branch policy',
      },
      {
        command: 'python3 "$(git rev-parse --show-toplevel)/.claude/hooks/worktree_scope_guard.py"',
        timeout: 20,
        statusMessage: 'Checking worktree cleanup scope policy (shared core)',
      },
      {
        command: `${checkCodexAgentsBase} --hook-pretool`,
        timeout: 30,
        statusMessage: 'Checking LOOP_PROTOCOL Bash guardrail',
      },
      {
        command: `${compositeBase} --event PreToolUse`,
        timeout: 30,
        statusMessage: 'Checking Codex session-recording PreToolUse guard',
      },
      {
        command: 'bash "$(git rev-parse --show-toplevel)/.codex/hooks/ci_test_performance_advisory.sh"',
        timeout: 10,
        statusMessage: 'Checking CI/test-lane path advisory',
      },
      {
        command: 'bash "$(git rev-parse --show-toplevel)/.codex/hooks/root_temporary_residue_advisory.sh"',
        timeout: 10,
        statusMessage: 'Checking root temporary residue advisory',
      },
    ],
  ],
  [
    '^(apply_patch|Edit|Write)$',
    [
      {
        command: `${checkCodexAgentsBase} --hook-pretool`,
        timeout: 30,
        statusMessage: 'Checking LOOP_PROTOCOL patch guardrail',
      },
      {
        command: `${compositeBase} --event PreToolUse`,
        timeout: 30,
        statusMessage: 'Checking Codex session-recording patch guard',
      },
      {
        command: 'bash "$(git rev-parse --show-toplevel)/.codex/hooks/ci_test_performance_advisory.sh"',
        timeout: 10,
        statusMessage: 'Checking CI/test-lane path advisory',
      },
      {
        command: 'bash "$(git rev-parse --show-toplevel)/.codex/hooks/root_temporary_residue_advisory.sh"',
        timeout: 10,
        statusMessage: 'Checking root temporary residue advisory',
      },
    ],
  ],
]);

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
  if (parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed)) {
    const rootKeys = Object.keys(parsed).sort();
    assert(
      JSON.stringify(rootKeys) === JSON.stringify(['hooks']),
      `hooks.json: root keys must be exactly ["hooks"], got ${JSON.stringify(rootKeys)}`,
      failures,
    );
  }
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
  assert(
    preToolEntries.length === expectedPreToolUseEntries.size,
    `hooks.json: PreToolUse must have exactly ${expectedPreToolUseEntries.size} matcher entries`,
    failures,
  );
  for (const [matcher, expectedHooks] of expectedPreToolUseEntries) {
    const entry = preToolEntries.find((candidate) => candidate?.matcher === matcher);
    assertExactHookEntry('PreToolUse', entry, matcher, expectedHooks, failures);
  }

  // AC3: structural validation for PermissionRequest, Stop, SubagentStop.
  // Each event must be a non-empty array; each entry must have at least one handler;
  // and each handler object must have a "command" field of type string.
  assert(Array.isArray(parsed?.hooks?.PermissionRequest), 'hooks.json: must have hooks.PermissionRequest array (AC3 #1020)', failures);
  assert(Array.isArray(parsed?.hooks?.Stop), 'hooks.json: must have hooks.Stop array (AC3 #1020)', failures);
  assert(Array.isArray(parsed?.hooks?.SubagentStop), 'hooks.json: must have hooks.SubagentStop array (AC3 #1020)', failures);
  const permissionRequestEntries = parsed?.hooks?.PermissionRequest ?? [];
  assert(permissionRequestEntries.length >= 1, 'hooks.json: PermissionRequest must have at least one entry (AC3 #1020)', failures);
  const stopEntries = parsed?.hooks?.Stop ?? [];
  assert(stopEntries.length >= 1, 'hooks.json: Stop must have at least one entry (AC3 #1020)', failures);
  const subagentStopEntries = parsed?.hooks?.SubagentStop ?? [];
  assert(subagentStopEntries.length >= 1, 'hooks.json: SubagentStop must have at least one entry (AC3 #1020)', failures);
  // Validate that each entry in PermissionRequest/Stop/SubagentStop has at least one handler
  // with a "command" field (string). An empty object {} or missing command fails.
  for (const [eventKey, entries] of [
    ['PermissionRequest', permissionRequestEntries],
    ['Stop', stopEntries],
    ['SubagentStop', subagentStopEntries],
  ]) {
    for (let i = 0; i < entries.length; i += 1) {
      const entry = entries[i];
      const handlers = entry?.hooks ?? [];
      assert(
        handlers.length >= 1,
        `hooks.json: ${eventKey}[${i}] must have at least one handler in hooks[] (AC3 #1020)`,
        failures,
      );
      for (let j = 0; j < handlers.length; j += 1) {
        assert(
          typeof handlers[j]?.command === 'string' && handlers[j].command.length > 0,
          `hooks.json: ${eventKey}[${i}].hooks[${j}] must have a non-empty "command" field (AC3 #1020)`,
          failures,
        );
      }
    }
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
    // read-only via rtk: must be allow
    { tokens: ['rtk', 'gh', 'issue', 'view', '1'], expectedDecision: 'allow' },
    { tokens: ['rtk', 'pnpm', 'test'], expectedDecision: 'allow' },
    // DIRECT_ALLOW: validation commands are now allow without rtk wrapper (#874)
    { tokens: ['pnpm', 'typecheck'], expectedDecision: 'allow' },
    { tokens: ['pnpm', 'lint'], expectedDecision: 'allow' },
    { tokens: ['pnpm', 'test'], expectedDecision: 'allow' },
    { tokens: ['pnpm', 'build'], expectedDecision: 'allow' },
    // DIRECT_ALLOW: git read-only is allow (#874)
    { tokens: ['git', 'status'], expectedDecision: 'allow' },
    { tokens: ['git', 'diff'], expectedDecision: 'allow' },
    { tokens: ['git', 'log'], expectedDecision: 'allow' },
    // DIRECT_ALLOW: gh read-only is allow (#874)
    { tokens: ['gh', 'issue', 'view', '1'], expectedDecision: 'allow' },
    { tokens: ['gh', 'pr', 'view', '1'], expectedDecision: 'allow' },
    { tokens: ['gh', 'pr', 'checks', '1'], expectedDecision: 'allow' },
    // mutations: must NOT be allow (prompt or forbidden)
    { tokens: ['rtk', 'gh', 'pr', 'merge', '1'], expectedDecision: 'prompt', expectNotAllow: true },
    { tokens: ['rtk', 'git', 'push'], expectedDecision: 'prompt', expectNotAllow: true },
    { tokens: ['rtk', 'pnpm', 'add', 'lodash'], expectedDecision: 'prompt', expectNotAllow: true },
    // direct bypass mutations: must be forbidden
    { tokens: ['git', 'push'], expectedDecision: 'forbidden' },
    { tokens: ['pnpm', 'add', 'lodash'], expectedDecision: 'forbidden' },
    { tokens: ['gh', 'pr', 'merge', '1'], expectedDecision: 'forbidden' },
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
  const bridgeSurfacePaths = [];
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
  const hookBoundariesPath = path.join(repoRoot, 'docs', 'dev', 'hook-boundaries.md');
  const skillBoundariesPath = path.join(repoRoot, 'docs', 'dev', 'agent-skill-boundaries.md');
  const hookBoundariesText = readOptionalText(hookBoundariesPath);
  const skillBoundariesText = readOptionalText(skillBoundariesPath);

  assert(configText.includes('default_permissions = "loop-protocol-rtk"'), 'config.toml must keep default_permissions = "loop-protocol-rtk"', failures);
  assert(configText.includes('[permissions.loop-protocol-readonly.filesystem]'), 'config.toml must define permissions.loop-protocol-readonly', failures);
  assert(configText.includes('.codex/hooks.json'), 'config.toml must mention .codex/hooks.json as the documented hook surface', failures);
  assert(!configText.includes('sandbox_mode'), 'config.toml must not use sandbox_mode when permission profiles are active', failures);
  assert(rulesText.includes('fail-closed local guardrail'), 'default.rules must describe hooks/rules as a fail-closed local guardrail', failures);
  assert(rulesText.includes('pattern = ["rtk", "git", "add"]'), 'default.rules must allow the bounded rtk git add publish path', failures);
  assert(rulesText.includes('pattern = ["rtk", "git", "commit", "-m"]'), 'default.rules must allow the bounded rtk git commit -m publish path', failures);
  assert(rulesText.includes('pattern = ["rtk", "git", "push", "origin"]'), 'default.rules must allow the bounded rtk git push origin publish path', failures);
  // AC1: config.toml must not define a [hooks] section; hooks live in .codex/hooks.json.
  // Also scan for TOML array-of-tables [[hooks.*]] which the parser skips to avoid false errors.
  // All of the following table header patterns must cause a failure:
  //   [hooks]  [hooks.PreToolUse]  [[hooks.PreToolUse]]  [[hooks.PreToolUse.hooks]]
  //   [[hooks.PermissionRequest]]  [[hooks.Stop]]  [[hooks.SubagentStop]]
  assert(!configParsed?.hooks, 'config.toml must not define a [hooks] section; use .codex/hooks.json instead (AC1 #1020)', failures);
  for (const line of configText.split(/\r?\n/)) {
    const stripped = line.trim();
    if (stripped.startsWith('#')) continue;
    // Match [hooks], [hooks.X], [[hooks.X]], [[hooks.X.Y]] etc.
    if (/^\[{1,2}hooks(\]|\.|\.{1,2}\w)/.test(stripped)) {
      failures.push(`config.toml must not define a hooks table/array-of-tables header "${stripped}"; use .codex/hooks.json instead (AC1 #1020)`);
    }
  }
  assert(rulesText.includes('Known limitation'), 'default.rules must mention Known limitation wording', failures);
  assert(fs.existsSync(path.join(repoRoot, 'scripts', 'agent-guards', 'git_mutation_command_policy.py')), 'git_mutation_command_policy.py must exist', failures);
  assert(fs.existsSync(path.join(repoRoot, 'scripts', 'agent-guards', 'hook_repair_hints.py')), 'hook_repair_hints.py must exist', failures);
  if (fs.existsSync(hookBoundariesPath)) {
    assert(hookBoundariesText.includes('HOOK_COMMAND_REPAIR_HINT_V1'), 'hook-boundaries.md must describe HOOK_COMMAND_REPAIR_HINT_V1', failures);
    for (const requiredField of [
      'blocked_command_class',
      'reason_code',
      'safe_action',
      'suggested_command',
      'forbidden_alternatives',
      'verification_command',
      'stop_condition',
    ]) {
      assert(hookBoundariesText.includes(requiredField), `hook-boundaries.md must mention repair hint field ${requiredField}`, failures);
    }
    for (const requiredReason of [
      'git_add_requires_explicit_pathspec',
      'git_add_outside_allowed_paths',
      'allowed_paths_missing_for_git_mutation',
      'commit_staged_changes_outside_allowed_paths',
      'push_refspec_requires_active_branch',
      'issue_context_required',
    ]) {
      assert(hookBoundariesText.includes(requiredReason), `hook-boundaries.md must mention repair hint reason ${requiredReason}`, failures);
    }
  }
  if (fs.existsSync(skillBoundariesPath)) {
    assert(skillBoundariesText.includes('agent steering'), 'agent-skill-boundaries.md must mention agent steering for repair hints', failures);
  }
  assert(!fs.existsSync(path.join(repoRoot, '.codex/skills')), '.codex/skills: must not exist as a repo-shared skill surface', failures);

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
    const runtimeFollowupRoute = extractRuntimeFollowupRoute(instructions);
    const skillSurfacePaths = extractSkillSurfacePaths(instructions);
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
    if (runtimeStatus === 'codex_skill_required') {
      assert(skillSurfacePaths.length > 0, `${file}: codex_skill_required agent must declare repo_local_skill_surface`, failures);
      const expectedSkillSurfacePaths = routeTokensToSkillSurfaces(runtimeFollowupRoute);
      assert(runtimeFollowupRoute !== null, `${file}: runtime_followup_route is required`, failures);
      assert(
        JSON.stringify(skillSurfacePaths) === JSON.stringify(expectedSkillSurfacePaths),
        `${file}: runtime_followup_route ${runtimeFollowupRoute} must map exactly to ${expectedSkillSurfacePaths.join(', ') || '(none)'}`,
        failures,
      );
      for (const skillSurfacePath of skillSurfacePaths) {
        assert(skillSurfacePath.startsWith('.agents/skills/'), `${file}: repo_local_skill_surface must stay under .agents/skills/`, failures);
        const fullSkillSurfacePath = path.join(repoRoot, skillSurfacePath);
        assert(fs.existsSync(fullSkillSurfacePath), `${file}: missing repo-local skill surface ${skillSurfacePath}`, failures);
        if (fs.existsSync(fullSkillSurfacePath)) {
          bridgeSurfacePaths.push(fullSkillSurfacePath);
          const body = readText(fullSkillSurfacePath);
          assert(body.includes('name:'), `${file}: ${skillSurfacePath} must declare name frontmatter`, failures);
          assert(body.includes('description:'), `${file}: ${skillSurfacePath} must declare description frontmatter`, failures);
          validateBridgeSurface(fullSkillSurfacePath, failures);
        }
      }
    }
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

  validateDuplicateCanonicalTargets([...new Set(bridgeSurfacePaths)], failures);

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

// Issue #1502 REQUEST_CHANGES (Blocker 1): tracks the single private
// work directory created by the in-flight `ensureLedgerWriter()` call (if
// any) so `writeLedgerEntry` can delete it once the child process has
// exited, regardless of success or failure. There is at most one pending
// work directory at a time -- `writeLedgerEntry` is not re-entrant.
let _pendingLedgerWriterWorkDir = null;

function _cleanupPendingLedgerWriterWorkDir() {
  if (_pendingLedgerWriterWorkDir) {
    fs.rmSync(_pendingLedgerWriterWorkDir, { recursive: true, force: true });
    _pendingLedgerWriterWorkDir = null;
  }
}

// Issue #1502 REQUEST_CHANGES (Blocker 1): validate that the OS temp root
// the private build directory will be created under is safe to trust,
// *before* creating anything there. Three independent checks:
//   1. an inherited `TMPDIR` env var must be an absolute path (a relative
//      value would resolve against an unpredictable/attacker-influenced
//      cwd);
//   2. `os.tmpdir()` (which honors `TMPDIR`), fully symlink-resolved via
//      `fs.realpathSync`, must not be the repo root or any path under it --
//      this also transitively rejects "symlink indirection into the repo"
//      because `realpathSync` resolves every symlink hop to its final
//      target;
//   3. the resolved temp root's own mode must not be world/group-writable
//      without the sticky bit (mirrors the standard `/tmp`-safety
//      precondition used by `mktemp`-family tooling: a world/group-writable
//      directory without the sticky bit lets any other same-machine process
//      rename/replace another user's -- or another same-uid session's --
//      entries out from under it, regardless of who currently owns the
//      directory node itself).
function _assertLedgerWriterTmpRootSafe() {
  const tmpdirEnv = process.env.TMPDIR;
  if (tmpdirEnv !== undefined && tmpdirEnv !== '' && !path.isAbsolute(tmpdirEnv)) {
    throw new Error('ledger_writer_tmpdir_env_relative');
  }
  const rawTmpRoot = os.tmpdir();
  if (!path.isAbsolute(rawTmpRoot)) {
    throw new Error('ledger_writer_tmp_root_not_absolute');
  }
  const tmpRootReal = fs.realpathSync(rawTmpRoot);
  const repoRootReal = fs.realpathSync(repoRoot);
  if (tmpRootReal === repoRootReal || tmpRootReal.startsWith(repoRootReal + path.sep)) {
    throw new Error('ledger_writer_tmp_root_inside_repo');
  }
  const tmpRootStat = fs.statSync(tmpRootReal);
  const worldOrGroupWritable = (tmpRootStat.mode & 0o022) !== 0;
  const stickyBit = (tmpRootStat.mode & 0o1000) !== 0;
  if (worldOrGroupWritable && !stickyBit) {
    throw new Error('ledger_writer_tmp_root_insecure_mode');
  }
  return tmpRootReal;
}

function ensureLedgerWriter() {
  const tmpRootReal = _assertLedgerWriterTmpRootSafe();
  // Read the exact source bytes once; the private source file compiled
  // below is written from these bytes directly, never by re-opening
  // `ledgerWriterSource` by pathname a second time (Issue #1502
  // REQUEST_CHANGES Blocker 1: reopening the same pathname after hashing it
  // would be a TOCTOU gap if the source file changed in between).
  const sourceBytes = fs.readFileSync(ledgerWriterSource);
  // Invocation-unique, unpredictable-path private directory -- never a
  // shared, content-addressed, predictable cache location.
  const workDir = fs.mkdtempSync(path.join(tmpRootReal, ledgerWriterPrivateDirPrefix));
  _pendingLedgerWriterWorkDir = workDir;
  try {
    const workDirReal = fs.realpathSync(workDir);
    if (workDirReal !== workDir) throw new Error('ledger_writer_workdir_symlink_component');
    const workDirStat = fs.lstatSync(workDir);
    if (workDirStat.isSymbolicLink() || !workDirStat.isDirectory()) throw new Error('ledger_writer_workdir_unsafe');
    if (typeof process.getuid === 'function' && workDirStat.uid !== process.getuid()) {
      throw new Error('ledger_writer_workdir_owner_mismatch');
    }
    if ((workDirStat.mode & 0o077) !== 0) throw new Error('ledger_writer_workdir_mode_unsafe');

    const privateSourcePath = path.join(workDir, 'subagent-launch-ledger-writer.c');
    fs.writeFileSync(privateSourcePath, sourceBytes, { mode: 0o600 });
    const binaryPath = path.join(workDir, 'ledger-writer');
    try {
      execFileSync('cc', ['-std=c17', '-Wall', '-Wextra', '-Werror', '-O2', '-o', binaryPath, privateSourcePath], { stdio: 'pipe' });
    } catch {
      throw new Error('ledger_writer_build_failed');
    }
    const binaryStat = fs.lstatSync(binaryPath);
    if (binaryStat.isSymbolicLink() || !binaryStat.isFile()) throw new Error('ledger_writer_binary_unsafe');
    return binaryPath;
  } catch (error) {
    _cleanupPendingLedgerWriterWorkDir();
    throw error;
  }
}

function writeLedgerEntry(kind, entry, identity) {
  let result;
  try {
    result = spawnSync(ensureLedgerWriter(), [
      '--repo', repoRoot,
      '--kind', kind,
      '--entry', JSON.stringify(entry),
      '--identity', identity,
    ], { encoding: 'utf8', timeout: 10_000, killSignal: 'SIGKILL' });
  } finally {
    // Issue #1502 REQUEST_CHANGES (Blocker 1): the private binary (and its
    // private source copy) is deleted once the child process has exited,
    // regardless of outcome -- it never persists as a reusable cache entry.
    _cleanupPendingLedgerWriterWorkDir();
  }
  if (result.error || result.status !== 0) {
    throw new Error(String(result.stderr || '').trim() || 'ledger_writer_failed');
  }
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
  writeLedgerEntry('launches', {
      agent_name: agentType,
      event_type: 'SubagentStart',
      evidence_source: 'event_derived',
      event_fingerprint: fingerprint,
      declared_runtime: {
        ...runtime,
        agent_definition_sha256: crypto.createHash('sha256').update(readText(path.join(agentsDir, `${agentType}.toml`))).digest('hex'),
      },
      observed_dispatch: {
        model: payload.model ?? payload.active_model ?? null,
        session_id: payload.session_id ?? null,
        turn_id: payload.turn_id ?? null,
        agent_id: payload.agent_id ?? null,
        observed_at: new Date().toISOString(),
      },
      correlation: {
        evidence_run_id: process.env.CODEX_AGENT_EVIDENCE_RUN_ID ?? null,
        repo_head_sha: process.env.CODEX_AGENT_EVIDENCE_HEAD_SHA ?? null,
        worktree_dirty: process.env.CODEX_AGENT_EVIDENCE_WORKTREE_DIRTY === 'true',
      },
  }, fingerprint);
}

function appendPreToolEvidence(payload, toolName, command) {
  const observedCommand = command || payload?.tool_input?.file_path || payload?.tool_input?.command || '';
  const action = {
    kind: classifyRootThreadAction(toolName, command),
    command: observedCommand,
    tool_name: toolName,
    coverage_source: 'supported_pretooluse_path',
  };
  writeLedgerEntry('root_thread_actions', action, `${action.tool_name}\n${action.command}`);
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

// Direct pnpm subcommands that are allowed without rtk wrapper (#874 AC).
// These are validation/build commands that are always safe to run directly.
const DIRECT_ALLOW_PNPM = /^pnpm\s+(typecheck|lint|test|build)(\s|$)/;

// Direct gh subcommands that are allowed without rtk wrapper (#874 AC).
const DIRECT_ALLOW_GH_ISSUE = /^gh\s+issue\s+(view|list)(\s|$)/;
const DIRECT_ALLOW_GH_PR = /^gh\s+pr\s+(view|list|checks|diff)(\s|$)/;

// Direct git read-only subcommands that are allowed without rtk wrapper (#874 AC).
const DIRECT_ALLOW_GIT_READONLY = /^git\s+(status|diff|log|branch|show|rev-parse|ls-files)(\s|$)/;

function isDirectBypass(command) {
  // Allow: read-only / validation pnpm commands
  if (DIRECT_ALLOW_PNPM.test(command)) return false;
  // Allow: read-only gh operations
  if (DIRECT_ALLOW_GH_ISSUE.test(command)) return false;
  if (DIRECT_ALLOW_GH_PR.test(command)) return false;
  // Allow: read-only git inspection
  if (DIRECT_ALLOW_GIT_READONLY.test(command)) return false;

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
// Protected-path enforcement (Issue #1612)
//
// The legacy env-driven Allowed Paths mode system (three env vars: one
// selecting a workspace/strict/shadow/unknown mode, one declaring a
// newline-separated allow-list, one legacy boolean alias for the default
// mode) has been fully removed from this file (Issue #1612 AC1-3): none of
// those env vars are read anywhere below, and setting them in an environment
// has no effect on the decisions below (Issue #1612 AC7, verified out of
// process by scripts/agent-guards/tests/test_codex_legacy_env_ignored.py).
// The editing-time filesystem boundary is now owned by two independent,
// non-overlapping mechanisms instead of an env-driven mode enum:
//   1. Native Codex permission profiles (`.codex/config.toml`
//      `[permissions.<profile>.filesystem]`), which govern general
//      read/write access at the process/sandbox level.
//   2. isProtectedPath() below, a validated mirror of the shared
//      PROTECTED_PATHS_POLICY_V1 JSON SSOT
//      (`scripts/agent-guards/protected_paths_policy.v1.json`, Issue #1611),
//      which always denies apply_patch/Edit/Write to assets/**, LICENSES/**,
//      .env*, secrets/** regardless of what an Issue's declared Allowed
//      Paths say.
//
// Per-Issue declared Allowed Paths are intentionally NOT narrowed by this
// hook: that enforcement now lives exclusively with the independent,
// `git diff`-derived allowed_paths_review_gate at PR review time (canonical)
// and, for git staging/commit itself, with the controlled stage/commit
// executor (`scripts/agent-guards/controlled_git_change_exec.py`, Issue
// #1611). Rollback: if `protected_paths_policy.v1.json` or this mirror is
// ever found to be broken/unreadable, `loadProtectedPathsPolicy()` throws
// and the hook process exits non-zero, which Codex's PreToolUse hook runner
// treats as a hook execution failure -- see
// "Rollback（障害時の安全側フォールバック）" in
// docs/dev/agent-runtime-ops.md for the documented recovery procedure.
//
// KNOWN LIMITATION: path canonicalization uses path.resolve without realpath,
// so symlinks that escape the repo root are not caught (WSL2 compatibility).
// ---------------------------------------------------------------------------

function resolveInsideRepo(inputPath) {
  if (!inputPath || inputPath.includes('\0')) return null;
  const resolved = path.resolve(repoRoot, inputPath);
  // Reject paths that escape the repo root
  if (resolved !== repoRoot && !resolved.startsWith(repoRoot + path.sep)) return null;
  return resolved;
}

let _protectedPathsPolicyCache = null;

/**
 * Load PROTECTED_PATHS_POLICY_V1 directly from the JSON SSOT (Issue #1611
 * contract revision, P1-2). Cached per-process (the file does not change
 * within a single hook invocation); throws (fail-closed, never silently
 * falls back to a hardcoded list) if the file is missing/malformed.
 */
function loadProtectedPathsPolicy() {
  if (_protectedPathsPolicyCache) return _protectedPathsPolicyCache;
  const raw = fs.readFileSync(protectedPathsPolicyPath, 'utf8');
  const data = JSON.parse(raw);
  if (data.schema !== 'PROTECTED_PATHS_POLICY_V1' || !Array.isArray(data.rules) || data.rules.length === 0) {
    throw new Error(`invalid protected paths policy at ${protectedPathsPolicyPath}`);
  }
  _protectedPathsPolicyCache = data;
  return data;
}

/** Returns true if filePath is a protected path (always denied). */
function isProtectedPath(filePath) {
  const candidate = resolveInsideRepo(filePath);
  if (!candidate) return true; // null path (traversal/NUL) → deny
  const policy = loadProtectedPathsPolicy();
  const basename = path.basename(filePath);
  for (const rule of policy.rules) {
    if (rule.kind === 'root_directory') {
      const dir = path.resolve(repoRoot, rule.path);
      if (candidate === dir || candidate.startsWith(dir + path.sep)) return true;
    } else if (rule.kind === 'basename_glob') {
      if (rule.pattern.endsWith('*') && !rule.pattern.slice(0, -1).includes('*')) {
        if (basename.startsWith(rule.pattern.slice(0, -1))) return true;
      } else if (basename === rule.pattern) {
        return true;
      }
    }
  }
  return false;
}

/** Legacy alias: same as isProtectedPath for backward compat. */
function isProtectedLegacy(filePath) {
  return isProtectedPath(filePath);
}

function extractPatchTouchedPaths(command) {
  return [...command.matchAll(/\*\*\* (?:Add|Delete|Update) File: (.+)$/gm)].map((match) => match[1].trim());
}

function denyForWriteTool(toolName, toolInput) {
  // apply_patch: extract touched paths from patch content
  if (toolName === 'apply_patch') {
    const command = normalizeCommand(toolInput?.command);
    const touchedPaths = extractPatchTouchedPaths(command);
    for (const filePath of touchedPaths) {
      if (isProtectedPath(filePath)) {
        return 'protected_path_violation: path is in a protected area (assets/, LICENSES/, .env*, secrets/**).';
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
    if (isProtectedPath(filePath)) {
      return 'protected_path_violation: path is in a protected area (assets/, LICENSES/, .env*, secrets/**).';
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
  let reason = null;

  if (supportedPreToolNames.includes(toolName)) {
    appendPreToolEvidence(payload, toolName, command);
  }

  if (toolName === 'Bash') {
    reason = denyForBash(command);
  } else if (toolName === 'apply_patch' || toolName === 'Edit' || toolName === 'Write') {
    reason = denyForWriteTool(toolName, toolInput);
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

  process.stdout.write('\n=== self-test: PROTECTED_PATHS_POLICY_V1 JSON SSOT mirrors ===\n');
  // Issue #1611 (contract revision, P1-2): every `root_directory` rule in the
  // JSON SSOT must appear as a read-only `.codex/config.toml` workspace_root
  // (the validated-mirror invariant this permission profile can represent).
  {
    const policy = loadProtectedPathsPolicy();
    const rootDirRules = policy.rules.filter((r) => r.kind === 'root_directory').map((r) => r.path);
    selfAssert(rootDirRules.length > 0, 'protected_paths_policy.v1.json: has at least one root_directory rule');
    // The bundled TOML parser here is a minimal subset (it does not parse
    // quoted map keys like `"assets" = "read"` into nested objects), so the
    // mirror check is done against the raw file text within each profile's
    // `:workspace_roots` table, not the parsed object.
    const configText = fs.readFileSync(configPath, 'utf8');
    for (const profileName of ['loop-protocol-rtk', 'loop-protocol-readonly', 'loop-protocol-bootstrap']) {
      const tableHeader = `[permissions.${profileName}.filesystem.":workspace_roots"]`;
      const headerIdx = configText.indexOf(tableHeader);
      selfAssert(headerIdx !== -1, `config.toml: has table ${tableHeader}`);
      const nextHeaderIdx = configText.indexOf('\n[', headerIdx + tableHeader.length);
      const tableBody = configText.slice(headerIdx, nextHeaderIdx === -1 ? undefined : nextHeaderIdx);
      for (const dir of rootDirRules) {
        selfAssert(
          new RegExp(`"${dir}"\\s*=\\s*"read"`).test(tableBody),
          `config.toml [permissions.${profileName}]: workspace_roots."${dir}" mirrors protected_paths_policy.v1.json (read-only)`,
        );
      }
    }
    // isProtectedPath() must agree with the JSON SSOT for every rule kind.
    selfAssert(isProtectedPath('assets/sprite.png') === true, 'isProtectedPath: assets/** matches JSON root_directory rule');
    selfAssert(isProtectedPath('secrets/token') === true, 'isProtectedPath: secrets/** matches JSON root_directory rule');
    selfAssert(isProtectedPath('.env') === true, 'isProtectedPath: .env matches JSON basename_glob rule');
    selfAssert(isProtectedPath('.env.production') === true, 'isProtectedPath: .env.* matches JSON basename_glob rule');
    selfAssert(isProtectedPath('src/main.ts') === false, 'isProtectedPath: unrelated path is not protected');
  }

  process.stdout.write('\n=== self-test: protected-path enforcement (Edit/Write) ===\n');

  // Test: normal path is allowed (no protected-path match)
  {
    const result = denyForWriteTool('Edit', { file_path: 'src/main.ts' });
    selfAssert(result === null, 'Normal path: src/main.ts edit is allowed');
  }

  // Test: assets/ is denied
  {
    const result = denyForWriteTool('Edit', { file_path: 'assets/sprite.png' });
    selfAssert(result !== null, 'Protected path: assets/ edit is denied');
  }

  // Test: LICENSES/ is denied
  {
    const result = denyForWriteTool('Edit', { file_path: 'LICENSES/MIT.txt' });
    selfAssert(result !== null, 'Protected path: LICENSES/ edit is denied');
  }

  // Test: .env is denied
  {
    const result = denyForWriteTool('Write', { file_path: '.env' });
    selfAssert(result !== null, 'Protected path: .env write is denied');
  }

  // Test: .env.local is denied
  {
    const result = denyForWriteTool('Write', { file_path: '.env.local' });
    selfAssert(result !== null, 'Protected path: .env.local write is denied');
  }

  // Test: secrets/ is denied
  {
    const result = denyForWriteTool('Write', { file_path: 'secrets/api-key.txt' });
    selfAssert(result !== null, 'Protected path: secrets/ write is denied');
  }

  // Test: path traversal that resolves inside a protected root is still denied
  {
    const result = denyForWriteTool('Write', { file_path: 'scripts/../secrets/token' });
    selfAssert(result !== null, 'Protected path: traversal resolving into secrets/ is denied');
  }

  // Test: path traversal that resolves outside the repo root is denied fail-closed
  {
    const result = denyForWriteTool('Write', { file_path: '../outside-repo/file.ts' });
    selfAssert(result !== null, 'Fail-closed: traversal escaping repo root is denied');
  }

  // Test: path traversal that stays inside the repo and lands on a normal path is allowed
  // (Allowed Paths narrowing is no longer this hook's responsibility -- see
  // allowed_paths_review_gate for the canonical, git-diff-derived enforcement.)
  {
    const result = denyForWriteTool('Write', { file_path: 'scripts/../src/main.ts' });
    selfAssert(result === null, 'Normal path: traversal resolving to a non-protected in-repo path is allowed');
  }

  // Test: NUL byte in path — denied fail-closed
  {
    const result = denyForWriteTool('Write', { file_path: 'scripts/foo\0bar.mjs' });
    selfAssert(result !== null, 'Fail-closed: NUL byte in path is denied');
  }

  process.stdout.write('\n=== self-test: protected-path enforcement (apply_patch) ===\n');

  // Test: apply_patch touching assets/ is denied
  {
    const patchCmd = '*** Update File: assets/sprite.png\n--- a\n+++ b\n';
    const result = denyForWriteTool('apply_patch', { command: patchCmd });
    selfAssert(result !== null, 'Protected path: apply_patch to assets/ is denied');
  }

  // Test: apply_patch touching secrets/ is denied
  {
    const patchCmd = '*** Update File: secrets/api-key.txt\n--- a\n+++ b\n';
    const result = denyForWriteTool('apply_patch', { command: patchCmd });
    selfAssert(result !== null, 'Protected path: apply_patch to secrets/ is denied');
  }

  // Test: apply_patch touching a normal path is allowed
  {
    const patchCmd = '*** Update File: scripts/check-codex-agents.mjs\n--- a\n+++ b\n';
    const result = denyForWriteTool('apply_patch', { command: patchCmd });
    selfAssert(result === null, 'Normal path: apply_patch to scripts/ is allowed');
  }

  // Test: apply_patch path traversal resolving into a protected root is denied
  {
    const patchCmd = '*** Update File: scripts/../.env\n--- a\n+++ b\n';
    const result = denyForWriteTool('apply_patch', { command: patchCmd });
    selfAssert(result !== null, 'Protected path: apply_patch traversal resolving into .env is denied');
  }

  process.stdout.write('\n=== self-test: Bash hook ===\n');

  // Test: rtk gh pr merge is NOT denied by bash hook (rtk prefix passes; policy enforced by execpolicy rules)
  {
    const result = denyForBash('rtk gh pr merge 1');
    selfAssert(result === null, 'Bash hook: rtk gh pr merge is not blocked (rtk prefix passes bash check)');
  }

  // Test: pnpm typecheck/lint/test/build direct → allow (#874 AC)
  {
    const result = denyForBash('pnpm typecheck');
    selfAssert(result === null, 'Bash hook: pnpm typecheck direct is allowed (#874)');
  }
  {
    const result = denyForBash('pnpm lint');
    selfAssert(result === null, 'Bash hook: pnpm lint direct is allowed (#874)');
  }
  {
    const result = denyForBash('pnpm test');
    selfAssert(result === null, 'Bash hook: pnpm test direct is allowed (#874)');
  }
  {
    const result = denyForBash('pnpm build');
    selfAssert(result === null, 'Bash hook: pnpm build direct is allowed (#874)');
  }

  // Test: git read-only commands direct → allow (#874 AC)
  {
    const result = denyForBash('git status');
    selfAssert(result === null, 'Bash hook: git status direct is allowed (#874)');
  }
  {
    const result = denyForBash('git diff');
    selfAssert(result === null, 'Bash hook: git diff direct is allowed (#874)');
  }
  {
    const result = denyForBash('git log --oneline -5');
    selfAssert(result === null, 'Bash hook: git log direct is allowed (#874)');
  }
  {
    const result = denyForBash('git branch --show-current');
    selfAssert(result === null, 'Bash hook: git branch direct is allowed (#874)');
  }

  // Test: gh read-only commands direct → allow (#874 AC)
  {
    const result = denyForBash('gh issue view 1');
    selfAssert(result === null, 'Bash hook: gh issue view direct is allowed (#874)');
  }
  {
    const result = denyForBash('gh pr checks 1');
    selfAssert(result === null, 'Bash hook: gh pr checks direct is allowed (#874)');
  }
  {
    const result = denyForBash('gh pr view 1');
    selfAssert(result === null, 'Bash hook: gh pr view direct is allowed (#874)');
  }

  // Test: pnpm mutation direct → deny (must still use rtk or be denied)
  {
    const result = denyForBash('pnpm add lodash');
    selfAssert(result !== null, 'Bash hook: pnpm add direct is denied (mutation)');
  }
  {
    const result = denyForBash('pnpm install');
    selfAssert(result !== null, 'Bash hook: pnpm install direct is denied (mutation)');
  }

  // Test: direct gh mutation → deny
  {
    const result = denyForBash('gh pr merge 1');
    selfAssert(result !== null, 'Bash hook: direct gh pr merge is denied');
  }
  {
    const result = denyForBash('gh issue create');
    selfAssert(result !== null, 'Bash hook: direct gh issue create is denied');
  }
  {
    const result = denyForBash('gh issue edit 1');
    selfAssert(result !== null, 'Bash hook: direct gh issue edit is denied');
  }
  {
    const result = denyForBash('gh pr create');
    selfAssert(result !== null, 'Bash hook: direct gh pr create is denied');
  }
  {
    const result = denyForBash('gh pr edit 1');
    selfAssert(result !== null, 'Bash hook: direct gh pr edit is denied');
  }
  {
    const result = denyForBash('gh pr review 1');
    selfAssert(result !== null, 'Bash hook: direct gh pr review is denied');
  }

  // Test: git push is denied
  {
    const result = denyForBash('git push');
    selfAssert(result !== null, 'Bash hook: git push is denied');
  }
  {
    const result = denyForBash('git commit -m test');
    selfAssert(result !== null, 'Bash hook: git commit is denied');
  }

  // Test: direct gh mutation denied with direct_bypass_requires_rtk reason
  {
    const result = denyForBash('gh pr merge 1');
    selfAssert(result !== null && result.includes('direct_bypass_requires_rtk'), 'Bash hook: direct gh denied with direct_bypass_requires_rtk reason');
  }

  // Test: hook payload JSON shape — pnpm typecheck via --hook-pretool input simulation
  // These verify that the hook processes JSON payload correctly (AC BLOCKER 2).
  {
    // Simulate the denyForBash path via normalizeCommand (which hook uses)
    const cmd = normalizeCommand('pnpm typecheck');
    const result = denyForBash(cmd);
    selfAssert(result === null, 'Bash hook (JSON payload): pnpm typecheck command from payload is allowed');
  }
  {
    const cmd = normalizeCommand('git status');
    const result = denyForBash(cmd);
    selfAssert(result === null, 'Bash hook (JSON payload): git status command from payload is allowed');
  }
  {
    const cmd = normalizeCommand('gh issue view 1');
    const result = denyForBash(cmd);
    selfAssert(result === null, 'Bash hook (JSON payload): gh issue view command from payload is allowed');
  }
  {
    const cmd = normalizeCommand('pnpm add lodash');
    const result = denyForBash(cmd);
    selfAssert(result !== null, 'Bash hook (JSON payload): pnpm add from payload is denied');
  }
  {
    const cmd = normalizeCommand('git push');
    const result = denyForBash(cmd);
    selfAssert(result !== null, 'Bash hook (JSON payload): git push from payload is denied');
  }
  {
    const cmd = normalizeCommand('gh pr merge 1');
    const result = denyForBash(cmd);
    selfAssert(result !== null, 'Bash hook (JSON payload): gh pr merge from payload is denied');
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
  if (arg === '--check-protected-path') {
    // Issue #1612 AC4: a stable, scriptable entry point so an out-of-process
    // parity test (scripts/agent-guards/tests/test_protected_paths_policy_node_parity.py)
    // can compare this Node mirror's isProtectedPath() decision against the
    // Python loader (scripts/agent-guards/protected_paths_policy.py) for the
    // exact same input, without duplicating either implementation's logic.
    const candidate = process.argv[3] ?? '';
    process.stdout.write(isProtectedPath(candidate) ? 'true\n' : 'false\n');
    return;
  }
  if (arg === '--check-write-tool') {
    // Issue #1612 AC7: a stable, scriptable entry point so an out-of-process
    // negative test (scripts/agent-guards/tests/test_codex_legacy_env_ignored.py)
    // can drive denyForWriteTool() directly -- with legacy env vars set in
    // the child process environment -- without also
    // triggering the unrelated SubagentStart launch-ledger evidence path
    // (appendPreToolEvidence()) that --hook-pretool always runs first.
    // Usage: --check-write-tool <toolName> <filePathOrPatchCommand>
    const toolName = process.argv[3] ?? '';
    const pathOrCommand = process.argv[4] ?? '';
    const toolInput = toolName === 'apply_patch' ? { command: pathOrCommand } : { file_path: pathOrCommand };
    const reason = denyForWriteTool(toolName, toolInput);
    process.stdout.write(reason ? `deny: ${reason}\n` : 'allow\n');
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
