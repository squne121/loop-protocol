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

**gh (read-only and managed skill mutations):**
- Allowed direct: `gh issue view`, `gh issue list`, `gh pr view`, `gh pr list`, `gh pr checks`, `gh pr diff`
- Allowed via managed skill (`create-issue` / `edit-issue` / `post-merge-cleanup`): `gh issue create/edit` with `--repo squne121/loop-protocol` and `--body-file tmp/<path>` required (github_issue_mutation_command)
- Destructive / bare mutation operations (`gh pr merge`, `gh pr create`, `gh pr checkout`, bare `gh issue create/edit` without required flags) remain forbidden without `rtk` or explicit human instruction
- See `docs/dev/hook-boundaries.md` for the full 5-class taxonomy (display_readonly_command / readonly_artifact_export_command / github_issue_mutation_command / github_pr_metadata_command / github_destructive_command)

## rtk trust boundary

- `rtk git` enforces direct mutating git operations within project policy.
- `rtk gh` controls GitHub write operations according to human approval / project policy.
- `rtk pnpm` enforces dependency mutation (install / add / remove / update) within project constraints.
- `rtk curl` and `rtk env` require explicit review due to secret/exfiltration risk.
- If `rtk` provides arbitrary shell passthrough without subcommand enforcement, this rules profile is not effective and must be renegotiated.

## エージェントランレポートと振り返りインデックス

エージェントランの完了後は以下の 3 つのアーティファクトで記録・分析を行う:

| アーティファクト | 責務 |
|---|---|
| `agent_session_manifest` | セッション中の内部追跡（読み取りファイル・ツール呼び出し） |
| `agent_run_report` (`agent_run_report/v1`) | 個別ランの公開可能要約・AC 達成状況・`evidence_refs`・`commands_summary` |
| `agent_retro_index` | 複数ランの横断インデックス・friction パターン・`follow_up_issues` 集約 |

詳細は `docs/dev/agent-run-report.md` を参照する。
レトロインデックスの schema は `docs/dev/agent-retro-index.md` を参照する。

各フェーズの Stop Conditions（`report finalized`、`public-safe check pass`、`posting dry-run or upsert done`）は
`docs/dev/agent-run-report.md` の「Phase Stop Conditions」セクションに定義されている。

## Hook Boundary と post-run verifier

hook は **diagnostic/prevention レイヤー** であり、カノニカルゲートではない。
**post-run verifier が canonical gate** である。
詳細は `docs/dev/agent-run-report.md` の「Hook Boundary Policy」セクションを参照する。

## agent ops read plan 例外

`agent-ops-review` タスクを実施する場合:
- `scripts/agent_ops_inventory.py --task-kind agent-ops-review --artifact-out /tmp/inv.json` で inventory を生成する
- stdout の `EVIDENCE:` 行が artifact path を示す
- MUST_READ / DO_NOT_READ_INITIAL_ONLY の詳細は `scripts/agent_ops_inventory.py --help` を参照する
