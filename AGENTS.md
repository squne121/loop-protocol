# AGENTS.md

## Codex execution policy

- After this file, read `CLAUDE.md` and follow its read order. This file only adds Codex-specific execution policy.
- Run project work through `rtk` (preferred harness).
- If the `rtk` interface is unclear, inspect it first with `rtk --help`.
- Do not modify `assets/` or `LICENSES/` unless the human explicitly authorizes it.
- Treat this as a stricter Codex profile than `.claude/settings.json`, not a byte-for-byte translation.
- Validation tasks should map to the repository scripts:
  - `typecheck`: `pnpm typecheck`
  - `lint`: `pnpm lint`
  - `test`: `pnpm test`
  - `build`: `pnpm build`

### Direct execution (bounded fallback)

`rtk` is the preferred harness, but the following commands are also allowed as direct execution when `rtk` wrappers are unavailable or inconvenient:

**pnpm (read/validation only):**
- `pnpm typecheck`, `pnpm lint`, `pnpm test`, `pnpm build` — direct execution allowed
- Mutation operations (`pnpm add`, `pnpm install`, `pnpm update`, `pnpm remove`) remain forbidden without `rtk` or explicit human instruction

**git (read-only inspection):**
- Allowed direct: `git status`, `git diff`, `git log`, `git branch`, `git show`, `git rev-parse`, `git ls-files`
- Mutation operations (`git push`, `git add`, `git commit`, `git checkout -b`, `git reset`, `git merge`) remain forbidden without `rtk` or explicit human instruction

**gh (read-only):**
- Allowed direct: `gh issue view`, `gh issue list`, `gh pr view`, `gh pr list`, `gh pr checks`, `gh pr diff`
- Mutation operations (`gh issue create`, `gh issue edit`, `gh pr merge`, `gh pr create`, `gh pr edit`) remain forbidden without `rtk` or explicit human instruction

## rtk trust boundary

- `rtk git` enforces direct mutating git operations within project policy.
- `rtk gh` controls GitHub write operations according to human approval / project policy.
- `rtk pnpm` enforces dependency mutation (install / add / remove / update) within project constraints.
- `rtk curl` and `rtk env` require explicit review due to secret/exfiltration risk.
- If `rtk` provides arbitrary shell passthrough without subcommand enforcement, this rules profile is not effective and must be renegotiated.
