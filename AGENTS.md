# AGENTS.md

## Codex execution policy

- Run project work through `rtk`.
- If the `rtk` interface is unclear, inspect it first with `rtk --help`.
- Do not bypass `rtk` with direct `pnpm`, mutating `git`, or `gh` commands unless the human explicitly instructs otherwise.
- Read-only git inspection is allowed: `git status`, `git diff`, `git branch --show-current`, `git log`.
- Do not modify `assets/` or `LICENSES/` unless the human explicitly authorizes it.
- Validation tasks should map to the repository scripts:
  - `typecheck`: `pnpm typecheck`
  - `lint`: `pnpm lint`
  - `test`: `pnpm test`
  - `build`: `pnpm build`
  These should be invoked through `rtk` when `rtk` provides wrappers.
