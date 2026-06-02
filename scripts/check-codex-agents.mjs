#!/usr/bin/env node

import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';

const repoRoot = process.cwd();
const agentsDir = path.join(repoRoot, '.codex', 'agents');
const configPath = path.join(repoRoot, '.codex', 'config.toml');
const hooksPath = path.join(repoRoot, '.codex', 'hooks.json');
const rulesPath = path.join(repoRoot, '.codex', 'rules', 'default.rules');

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
const reasoningMap = new Map([
  ['codebase-investigator', { model: 'codex-mini-latest', effort: 'medium' }],
  ['implementation-worker', { model: 'gpt-5-codex', effort: 'high' }],
  ['issue-author', { model: 'gpt-5-codex', effort: 'medium' }],
  ['issue-reviewer', { model: 'codex-mini-latest', effort: 'medium' }],
  ['post-merge-cleanup-worker', { model: 'codex-mini-latest', effort: 'medium' }],
  ['pr-reviewer-lite', { model: 'codex-mini-latest', effort: 'medium' }],
  ['pr-reviewer', { model: 'gpt-5-codex', effort: 'high' }],
  ['review-issue', { model: 'gpt-5-codex', effort: 'high' }],
  ['test-runner', { model: 'codex-mini-latest', effort: 'medium' }],
  ['web-researcher', { model: 'codex-mini-latest', effort: 'medium' }],
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

function readText(filePath) {
  return fs.readFileSync(filePath, 'utf8');
}

function fail(message) {
  process.stderr.write(`${message}\n`);
  process.exit(1);
}

function parseTomlFile(filePath) {
  const text = readText(filePath);
  const result = {};
  const lines = text.split(/\r?\n/);
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i].trim();
    if (!line || line.startsWith('#')) {
      continue;
    }
    const match = /^([A-Za-z0-9_.-]+)\s*=\s*(.+)$/.exec(line);
    if (!match) {
      continue;
    }
    const [, key, rawValue] = match;
    if (rawValue === '"""') {
      const collected = [];
      i += 1;
      while (i < lines.length && lines[i] !== '"""') {
        collected.push(lines[i]);
        i += 1;
      }
      result[key] = collected.join('\n');
      continue;
    }
    if (rawValue.startsWith('"""')) {
      const remainder = rawValue.slice(3);
      if (remainder.endsWith('"""')) {
        result[key] = remainder.slice(0, -3);
      } else {
        const collected = [remainder];
        i += 1;
        while (i < lines.length && lines[i] !== '"""') {
          collected.push(lines[i]);
          i += 1;
        }
        result[key] = collected.join('\n');
      }
      continue;
    }
    if (rawValue.startsWith('"')) {
      result[key] = JSON.parse(rawValue);
      continue;
    }
    result[key] = rawValue;
  }
  return result;
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

function validateAgents() {
  const failures = [];
  const files = getAgentFiles();
  assert(files.length === requiredAgentNames.length, `expected ${requiredAgentNames.length} agent files, found ${files.length}`, failures);

  const seenNames = new Set();
  const configText = readText(configPath);
  const rulesText = readText(rulesPath);
  const hooksText = readText(hooksPath);

  assert(configText.includes('default_permissions = "loop-protocol-rtk"'), 'config.toml must keep default_permissions = "loop-protocol-rtk"', failures);
  assert(configText.includes('[permissions.loop-protocol-readonly.filesystem]'), 'config.toml must define permissions.loop-protocol-readonly', failures);
  assert(configText.includes('.codex/hooks.json'), 'config.toml must mention .codex/hooks.json as the documented hook surface', failures);
  assert(!configText.includes('sandbox_mode'), 'config.toml must not use sandbox_mode when permission profiles are active', failures);
  assert(rulesText.includes('fail-closed local guardrail'), 'default.rules must describe hooks/rules as a fail-closed local guardrail', failures);
  assert(rulesText.includes('Known limitation'), 'default.rules must mention Known limitation wording', failures);
  assert(hooksText.includes('rtk pnpm exec node'), 'hooks.json must invoke the validator through rtk pnpm exec node', failures);
  assert(hooksText.includes('scripts/check-codex-agents.mjs'), 'hooks.json must route through scripts/check-codex-agents.mjs', failures);
  assert(hooksText.includes('$(git rev-parse --show-toplevel)'), 'hooks.json must resolve from git root', failures);

  for (const requiredName of requiredAgentNames) {
    const filename = `${requiredName}.toml`;
    assert(files.includes(filename), `missing agent file: ${filename}`, failures);
  }

  for (const file of files) {
    const filePath = path.join(agentsDir, file);
    const parsed = parseTomlFile(filePath);
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

  return { failures };
}

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
  if (/^rtk\b/.test(command) || !command) {
    return null;
  }
  if (isDirectBypass(command)) {
    return 'Use repo-documented rtk wrappers instead of direct pnpm / gh / mutating git commands.';
  }
  return null;
}

function denyForPatch(command) {
  const touchedPaths = [...command.matchAll(/\*\*\* (?:Add|Delete|Update) File: (.+)$/gm)].map((match) => match[1].trim());
  if (touchedPaths.some((filePath) => filePath === 'assets' || filePath.startsWith('assets/') || filePath === 'LICENSES' || filePath.startsWith('LICENSES/'))) {
    return 'assets/ and LICENSES/ are human-managed and blocked by the local guardrail.';
  }
  return null;
}

function runPreToolHook() {
  const payload = parseHookInput();
  const toolName = payload.tool_name;
  const command = normalizeCommand(payload?.tool_input?.command);
  let reason = null;

  if (toolName === 'Bash') {
    reason = denyForBash(command);
  } else if (toolName === 'apply_patch') {
    reason = denyForPatch(command);
  }

  if (reason) {
    hookDeny(reason);
  }
}

function runSubagentStartHook() {
  const payload = parseHookInput();
  const agentType = payload.agent_type ?? 'unknown-agent';
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
