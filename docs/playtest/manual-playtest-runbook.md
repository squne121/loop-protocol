---
doc_id: manual-playtest-runbook
status: active
issue: "#561"
parent_issue: "#543"
required_os: "WSL2 + Ubuntu"
date: "2026-06-02"
---

# Manual Playtest Runbook — WSL2/Ubuntu

## Online Playtest URLs (GitHub Pages)

You can run a manual playtest directly from a browser without any local setup.

### Main (shared) URL

After a commit is merged to `main`, the latest build is published to:

```
https://squne121.github.io/loop-protocol/
```

### PR Preview URL

When a pull request is opened or updated, a preview is deployed to:

```
https://squne121.github.io/loop-protocol/pr-<PR番号>/
```

Replace `<PR番号>` with the numeric PR number. For example, PR #123 becomes:
`https://squne121.github.io/loop-protocol/pr-123/`

The preview URL is also posted as a comment on the PR automatically.

> **Note:** The PR preview is automatically deleted when the PR is closed.
> After the PR is closed, the preview URL (`/pr-<PR番号>/`) will return 404.

> **Note:** PR preview is only generated for same-repository PRs. PRs opened from
> forks do not receive a preview deployment.

### Prerequisites (first-time setup by repo maintainer)

GitHub Pages must be configured to serve from the `gh-pages` branch:
`Settings -> Pages -> Source -> Deploy from a branch -> gh-pages (root)`

This is a one-time manual step by the repository owner. After setup, all deploys are automated.

---

This runbook guides a human tester through a manual playtest session on WSL2/Ubuntu.
Follow steps in order. If any step fails, refer to the [Troubleshooting](#common-failure-cases-and-remedies) section.

## Required OS/Runtime

- **Required OS/runtime: WSL2 + Ubuntu** (primary path)
- Node: v20 or later (v22+ recommended)
- pnpm: v9–v10 (compatible with Node 20) or v11+ (with Node 22+)

Optional (WSLg path): Linux GUI browser (e.g., Firefox on Ubuntu with WSLg)

---

## Step 1: Environment Preflight

Run the preflight script to verify your environment before starting:

```bash
node scripts/check-manual-playtest-env.mjs
```

Expected output: `[ok] All checks passed.` with exit code 0.

If the exit code is 1 (fail) or 2 (unsupported), follow the remedies in the [Troubleshooting](#common-failure-cases-and-remedies) section before continuing.

---

## Step 2: Install Dependencies

```bash
pnpm install
```

This installs all Node dependencies. Skip if `node_modules/` is already up to date.

---

## Step 3: Build the Production Bundle

```bash
pnpm build
```

This runs TypeScript compilation followed by Vite build. The output is placed in `dist/`.

---

## Step 4: Start the Preview Server

```bash
pnpm preview -- --host 127.0.0.1 --port 4173 --strictPort
```

- `--host 127.0.0.1`: Binds to localhost only (WSL2 loopback, accessible from Windows browser via `localhost`)
- `--port 4173`: Fixed port for playtest
- `--strictPort`: Fail immediately if port 4173 is already in use (prevents silent port shifting)

Expected output:

```
  ➜  Local:   http://127.0.0.1:4173/
```

Keep this terminal open during the entire playtest session.

### Expected Ports and Host Binding

| Binding | Port | Protocol |
|---------|------|----------|
| `127.0.0.1` (primary) | `4173` | HTTP |

The `--strictPort` flag is mandatory. If the server starts on a different port, the test is invalid.

---

## Step 5: Access in Browser

### Primary Browser Route (Windows Browser → `http://localhost:4173`)

Open a browser on your **Windows host** (Edge, Chrome, Firefox) and navigate to:

```
http://localhost:4173
```

WSL2 automatically bridges `localhost` from Windows to the WSL2 loopback interface. No additional configuration is required.

This is the **primary browser route** for manual playtest.

### Optional Browser Route (WSLg / Linux Browser)

If your system has WSLg (Windows Subsystem for Linux GUI) installed, you can use a Linux browser (e.g., Firefox, Chromium) running inside WSL2:

```bash
# Example: launch Firefox from Ubuntu terminal (WSLg required)
firefox http://localhost:4173 &
```

This route is **optional**. If WSLg is not available, use the Windows browser route above.

---

## Step 6: Execute Manual Checklist

With the game loaded in your browser, verify the items in `docs/playtest/m2-combat-mvp.md` under **Manual Checklist**:

1. WASD movement — player moves in all 4 directions
2. Mouse click fires projectiles toward cursor
3. Enemy spawns within a few seconds of sortie start
4. Enemy moves toward the player
5. Projectile hits reduce enemy HP / enemy is defeated
6. Contact damage reduces player HP
7. Victory: all enemies defeated → victory status shown in HUD
8. Defeat (timeout): 30s elapse → defeat status shown
9. Defeat (HP): player HP = 0 → defeat status shown

Mark each checklist item with your observation result.

---

## Step 7: Screenshot and Video Evidence Capture

After completing the manual checklist, capture evidence for the playtest record.

### Screenshot (Windows)

- **Windows Snipping Tool**: Press `Win + Shift + S` to capture a region screenshot.
- **Full-screen screenshot**: Press `PrtScn` or `Win + PrtScn` (saved to `Pictures/Screenshots`).
- **Browser DevTools**: Right-click on the game canvas → Inspect → Console → `document.querySelector('canvas').toDataURL()` for inline base64 screenshot.

### Screenshot (WSLg / Linux)

```bash
# gnome-screenshot (if available)
gnome-screenshot -a -f ~/playtest-screenshot.png

# scrot (if available)
scrot -s ~/playtest-screenshot.png
```

### Video Evidence

- **Windows**: Use Xbox Game Bar (`Win + G`) → Capture → Start recording while the game is open in the browser.
- **OBS Studio**: Open OBS, add a Window Capture source pointing to your browser, and record a short clip.
- **WSLg**: Use `recordmydesktop` or similar tools if available.

### Saving Evidence

Save screenshots and videos with filenames that include the date and issue number, for example:

```
playtest-561-2026-06-02-movement.png
playtest-561-2026-06-02-victory.mp4
```

Add these files to `docs/playtest/` or attach them to the related GitHub issue/PR comment.

---

## Step 8: Record Results in m2-combat-mvp.md

Update `docs/playtest/m2-combat-mvp.md` with your playtest results:

1. Check off completed items in the **Manual Checklist** section.
2. Add a new entry under `observed.human_playtest` (YAML block) with:
   - `tester`: your GitHub handle
   - `date`: ISO date
   - `platform`: `WSL2 + Ubuntu`
   - `browser`: browser name and version
   - `result`: `pass` / `partial` / `fail`
   - `notes`: any observations or issues found
3. Attach screenshot/video filenames or links to the same PR comment.

---

## Common Failure Cases and Remedies

### `pnpm: command not found`

Install pnpm via corepack. Use the version appropriate for your Node major:

```bash
# Node 20.x
corepack enable pnpm
corepack prepare pnpm@latest-10 --activate

# Node 22+
corepack enable pnpm
corepack prepare pnpm@latest-11 --activate
```

Avoid `pnpm@latest` on Node 20 — it may resolve pnpm 11 which requires Node 22+.

Or install globally with npm:

```bash
npm install -g pnpm
```

### `Port 4173 is already in use` (strictPort error)

Find and kill the process occupying port 4173:

```bash
lsof -ti:4173 | xargs kill -9
```

Then retry `pnpm preview -- --host 127.0.0.1 --port 4173 --strictPort`.

### Browser shows `ERR_CONNECTION_REFUSED` on `http://localhost:4173`

1. Confirm the preview server is running (check your terminal for the `➜ Local: http://127.0.0.1:4173/` line).
2. Verify you are using `http://` not `https://`.
3. If using Windows browser, check if a VPN or firewall is blocking localhost access.
4. As a fallback, try `http://127.0.0.1:4173` directly in the Windows browser.

### `pnpm build` fails with TypeScript errors

Run `pnpm typecheck` first to get readable error messages:

```bash
pnpm typecheck
```

Fix any reported errors before retrying `pnpm build`.

### WSL2 `localhost` not forwarding to Windows

WSL2 typically forwards localhost automatically on Windows 11 and modern Windows 10.
If forwarding does not work:

1. Open PowerShell (Admin) and run (use `wsl.exe -l -v` first to confirm your distro name):
   ```powershell
   $wslIp = (wsl.exe -d Ubuntu hostname -I).Trim().Split()[0]
   netsh interface portproxy add v4tov4 listenport=4173 listenaddress=0.0.0.0 connectport=4173 connectaddress=$wslIp
   ```
2. Access via the WSL2 IP directly in the Windows browser: `http://<wsl2-ip>:4173`

### Windows browser cannot reach WSL2 localhost even though preview is running

Check `%UserProfile%\.wslconfig` on Windows. If `[wsl2] localhostForwarding=false`
is set, remove it or set it to `true`, then run:

```powershell
wsl --shutdown
```

Restart Ubuntu and rerun the preflight + preview command.

### `check-manual-playtest-env.mjs` exits with code 2 (unsupported)

The preflight script detected a non-WSL2 environment (e.g., native Linux, macOS, Windows CMD).
This runbook is designed for WSL2/Ubuntu. Use the appropriate runbook for your OS.

### `pnpm-lock.yaml` not found

The lock file is missing. Run:

```bash
pnpm install
```

This regenerates `pnpm-lock.yaml`. If the issue persists, check if you are in the correct repository root directory.

---

## Reference

- Playtest log: `docs/playtest/m2-combat-mvp.md`
- Preflight script: `scripts/check-manual-playtest-env.mjs`
- Issue: #561 (this runbook), #543 (human manual playtest requirement)
