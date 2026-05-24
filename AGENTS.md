# AGENTS.md

## Codex execution policy

- After this file, read `CLAUDE.md` and follow its read order. This file only adds Codex-specific execution policy.
- Run project work through `rtk`.
- If the `rtk` interface is unclear, inspect it first with `rtk --help`.
- Do not bypass `rtk` with direct `pnpm`, mutating `git`, or `gh` commands unless the human explicitly instructs otherwise.
- Read-only git inspection is allowed: `git status`, `git diff`, `git branch --show-current`, `git log`.
- Do not modify `assets/` or `LICENSES/` unless the human explicitly authorizes it.
- Treat this as a stricter Codex profile than `.claude/settings.json`, not a byte-for-byte translation.
- Validation tasks should map to the repository scripts:
  - `typecheck`: `pnpm typecheck`
  - `lint`: `pnpm lint`
  - `test`: `pnpm test`
  - `build`: `pnpm build`
  These must be invoked through `rtk` when `rtk` provides wrappers (rules forbid direct `pnpm` execution).

## rtk trust boundary

- `rtk git` enforces direct mutating git operations within project policy.
- `rtk gh` controls GitHub write operations according to human approval / project policy.
- `rtk pnpm` enforces dependency mutation (install / add / remove / update) within project constraints.
- `rtk curl` and `rtk env` require explicit review due to secret/exfiltration risk.
- If `rtk` provides arbitrary shell passthrough without subcommand enforcement, this rules profile is not effective and must be renegotiated.
