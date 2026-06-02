#!/usr/bin/env node
/**
 * check-manual-playtest-env.mjs
 *
 * Preflight script for manual playtest on WSL2/Ubuntu.
 * Checks environment requirements without running `pnpm install` or `pnpm build`.
 *
 * Exit codes:
 *   0 = all checks passed (ok)
 *   1 = one or more checks failed (fail)
 *   2 = unsupported environment (non-WSL2)
 */

import { createServer } from 'net';
import { existsSync, readFileSync } from 'fs';
import { execSync } from 'child_process';

const PREVIEW_HOST = '127.0.0.1';
const PREVIEW_PORT = 4173;

let allPassed = true;
const results = [];

function pass(label, detail) {
  results.push({ status: 'pass', label, detail });
}

function fail(label, detail) {
  allPassed = false;
  results.push({ status: 'fail', label, detail });
}

function warn(label, detail) {
  results.push({ status: 'warn', label, detail });
}

// --- WSL2 Detection ---
function detectWSL2() {
  const wslDistro = process.env.WSL_DISTRO_NAME;
  if (wslDistro) {
    return { detected: true, method: 'WSL_DISTRO_NAME', value: wslDistro };
  }
  try {
    const procVersion = readFileSync('/proc/version', 'utf8');
    if (/microsoft/i.test(procVersion)) {
      return { detected: true, method: '/proc/version', value: procVersion.trim().slice(0, 80) };
    }
  } catch {
    // /proc/version not readable (non-Linux)
  }
  return { detected: false };
}

// --- Node Version Check ---
// Supported: Node 20/pnpm 9–10 compatible OR Node 22+/pnpm 11+ compatible
function checkNodeVersion() {
  const major = process.versions.node ? parseInt(process.versions.node.split('.')[0], 10) : 0;
  if (major >= 20) {
    pass('node-version', `Node ${process.versions.node} (>= 20 required)`);
    return true;
  } else {
    fail('node-version', `Node ${process.versions.node} — requires Node 20+ (pnpm 9–10) or Node 22+ (pnpm 11+)`);
    return false;
  }
}

// --- pnpm Availability ---
function checkPnpmAvailability() {
  try {
    const version = execSync('pnpm --version', { stdio: ['pipe', 'pipe', 'pipe'] }).toString().trim();
    pass('pnpm-available', `pnpm ${version} found`);
    return true;
  } catch {
    fail('pnpm-available', 'pnpm not found in PATH — install via `corepack enable && corepack prepare pnpm@latest --activate`');
    return false;
  }
}

// --- pnpm-lock.yaml Existence ---
function checkLockfile() {
  if (existsSync('pnpm-lock.yaml')) {
    pass('pnpm-lockfile', 'pnpm-lock.yaml exists');
    return true;
  } else {
    fail('pnpm-lockfile', 'pnpm-lock.yaml not found — run `pnpm install` to generate it');
    return false;
  }
}

// --- package.json build script ---
function checkBuildScript() {
  if (!existsSync('package.json')) {
    fail('build-script', 'package.json not found — are you in the repository root?');
    return false;
  }
  try {
    const pkg = JSON.parse(readFileSync('package.json', 'utf8'));
    if (pkg.scripts && typeof pkg.scripts.build === 'string') {
      pass('build-script', `"build" script present: ${pkg.scripts.build}`);
      return true;
    } else {
      fail('build-script', 'No "build" script found in package.json scripts');
      return false;
    }
  } catch (e) {
    fail('build-script', `Failed to parse package.json: ${e.message}`);
    return false;
  }
}

// --- Preview Port 4173 Bind Check ---
// Uses net module to probe 127.0.0.1:4173 availability (non-destructive)
function checkPreviewPort() {
  return new Promise((resolve) => {
    const server = createServer();
    server.once('error', (err) => {
      if (err.code === 'EADDRINUSE') {
        fail('preview-port', `Port ${PREVIEW_PORT} on ${PREVIEW_HOST} is already in use — free it before running preview`);
      } else {
        fail('preview-port', `Failed to probe port ${PREVIEW_PORT}: ${err.message}`);
      }
      resolve(false);
    });
    server.once('listening', () => {
      server.close(() => {
        pass('preview-port', `Port ${PREVIEW_PORT} on ${PREVIEW_HOST} is available`);
        resolve(true);
      });
    });
    server.listen(PREVIEW_PORT, PREVIEW_HOST);
  });
}

// --- Main ---
async function main() {
  console.log('[check-manual-playtest-env] Starting preflight checks...\n');

  // WSL2 detection — if not WSL2, exit 2 (unsupported)
  const wsl = detectWSL2();
  if (!wsl.detected) {
    console.log('[unsupported] Not running in WSL2 environment.');
    console.log('  This runbook and preflight script targets WSL2 + Ubuntu.');
    console.log('  Detected platform: ' + process.platform);
    console.log('  WSL_DISTRO_NAME: ' + (process.env.WSL_DISTRO_NAME || '(not set)'));
    process.exit(2);
  }
  pass('wsl2-detected', `WSL2 detected via ${wsl.method}: ${wsl.value}`);

  // Synchronous checks
  checkNodeVersion();
  checkPnpmAvailability();
  checkLockfile();
  checkBuildScript();

  // Async port check
  await checkPreviewPort();

  // Print results
  console.log('');
  for (const r of results) {
    const icon = r.status === 'pass' ? '[pass]' : r.status === 'warn' ? '[warn]' : '[fail]';
    console.log(`${icon} ${r.label}: ${r.detail}`);
  }
  console.log('');

  const failed = results.filter((r) => r.status === 'fail');
  const warned = results.filter((r) => r.status === 'warn');

  if (failed.length === 0) {
    console.log('[ok] All checks passed.');
    process.exit(0);
  } else {
    console.log(`[fail] ${failed.length} check(s) failed.${warned.length > 0 ? ` ${warned.length} warning(s).` : ''}`);
    console.log('See docs/playtest/manual-playtest-runbook.md for remedies.');
    process.exit(1);
  }
}

main().catch((err) => {
  console.error('[error] Unexpected error:', err.message);
  process.exit(1);
});
