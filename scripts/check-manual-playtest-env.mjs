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
// Distinguishes WSL2 from WSL1 and non-WSL environments.
// WSL2: /proc/sys/kernel/osrelease contains 'WSL2' or 'microsoft' (lowercase), AND /proc/version contains 'WSL2'
// WSL1: /proc/version contains 'Microsoft' but NOT 'WSL2'
// Returns { detected: boolean, wsl2: boolean, method, value }
function detectWSL2() {
  let osrelease = '';
  let procVersion = '';

  try {
    osrelease = readFileSync('/proc/sys/kernel/osrelease', 'utf8').trim();
  } catch {
    // Not readable (non-Linux or permission denied)
  }

  try {
    procVersion = readFileSync('/proc/version', 'utf8').trim();
  } catch {
    // Not readable
  }

  // WSL2 detection: osrelease contains 'WSL2' or 'microsoft' (case-insensitive)
  // AND (procVersion contains 'WSL2' OR osrelease contains 'WSL2')
  const osreleaseIsWSL2 = /WSL2/i.test(osrelease) || /microsoft/i.test(osrelease);
  const procVersionIsWSL2 = /WSL2/i.test(procVersion);
  const isWSL2 = osreleaseIsWSL2 || procVersionIsWSL2;

  // WSL1 detection: procVersion has 'Microsoft' but no 'WSL2' signature
  const isWSL1 = /microsoft/i.test(procVersion) && !isWSL2;

  if (isWSL2) {
    const method = osreleaseIsWSL2 ? '/proc/sys/kernel/osrelease' : '/proc/version';
    const value = osreleaseIsWSL2 ? osrelease : procVersion.slice(0, 80);
    return { detected: true, wsl2: true, wsl1: false, method, value };
  }

  if (isWSL1) {
    return { detected: true, wsl2: false, wsl1: true, method: '/proc/version', value: procVersion.slice(0, 80) };
  }

  // WSL_DISTRO_NAME without confirmed WSL2 kernel — treat as unknown, fail-closed
  const wslDistro = process.env.WSL_DISTRO_NAME;
  if (wslDistro && !isWSL1) {
    // Could not confirm WSL2 via kernel — fail-closed
    return { detected: false, wsl2: false, wsl1: false, method: 'WSL_DISTRO_NAME-unconfirmed', value: wslDistro };
  }

  return { detected: false, wsl2: false, wsl1: false };
}

// --- Ubuntu Detection ---
// Reads /etc/os-release and checks for ID=ubuntu
function detectUbuntu() {
  try {
    const osRelease = readFileSync('/etc/os-release', 'utf8');
    const idMatch = osRelease.match(/^ID=(.+)$/m);
    if (idMatch) {
      const id = idMatch[1].replace(/["']/g, '').toLowerCase();
      return { isUbuntu: id === 'ubuntu', id };
    }
  } catch {
    // /etc/os-release not readable
  }
  return { isUbuntu: false, id: 'unknown' };
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

// --- pnpm Availability and Compatibility ---
// Compatibility matrix:
//   Node 20 + pnpm 9–10: supported
//   Node 22+ + pnpm 9–11: supported
//   pnpm 11+ requires Node 22+
//   pnpm <= 8: too old
function checkPnpmAvailability() {
  let pnpmVersion;
  try {
    pnpmVersion = execSync('pnpm --version', { stdio: ['pipe', 'pipe', 'pipe'] }).toString().trim();
  } catch {
    fail('pnpm-available', 'pnpm not found in PATH — install via `corepack enable pnpm && corepack prepare pnpm@latest-10 --activate` (Node 20) or `pnpm@latest-11` (Node 22+)');
    return false;
  }

  const pnpmMajor = parseInt(pnpmVersion.split('.')[0], 10);
  const nodeMajor = process.versions.node ? parseInt(process.versions.node.split('.')[0], 10) : 0;

  if (pnpmMajor >= 11 && nodeMajor < 22) {
    fail(
      'node-pnpm-compatibility',
      `pnpm ${pnpmVersion} requires Node 22+; current Node is ${process.versions.node}`,
    );
    return false;
  }
  if (pnpmMajor <= 8) {
    fail(
      'node-pnpm-compatibility',
      `pnpm ${pnpmVersion} is too old; need pnpm 9+ (Node 20) or pnpm 11+ (Node 22+)`,
    );
    return false;
  }

  pass('pnpm-available', `pnpm ${pnpmVersion} found (node ${process.versions.node} — compatible)`);
  return true;
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

  // WSL2 detection — if not confirmed WSL2, exit 2 (unsupported)
  const wsl = detectWSL2();
  if (!wsl.detected || !wsl.wsl2) {
    console.log('[unsupported] Not running in a confirmed WSL2 environment.');
    console.log('  This runbook and preflight script targets WSL2 + Ubuntu.');
    console.log('  Detected platform: ' + process.platform);
    if (wsl.wsl1) {
      console.log('  Detected WSL1 (not WSL2) — upgrade to WSL2: `wsl --set-version <distro> 2`');
    } else {
      console.log('  WSL_DISTRO_NAME: ' + (process.env.WSL_DISTRO_NAME || '(not set)'));
      console.log('  Could not confirm WSL2 kernel via /proc/sys/kernel/osrelease or /proc/version.');
    }
    process.exit(2);
  }
  pass('wsl2-detected', `WSL2 confirmed via ${wsl.method}: ${wsl.value}`);

  // Ubuntu detection — if not Ubuntu, exit 2 (unsupported)
  const ubuntu = detectUbuntu();
  if (!ubuntu.isUbuntu) {
    console.log(`[unsupported] Non-Ubuntu distro detected: ID=${ubuntu.id}`);
    console.log('  This runbook targets Ubuntu on WSL2. Other distros are not supported.');
    process.exit(2);
  }
  pass('ubuntu-detected', `Ubuntu distro confirmed (ID=${ubuntu.id}`);

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
