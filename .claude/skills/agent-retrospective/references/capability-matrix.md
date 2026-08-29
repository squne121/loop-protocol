# Reused Agent Capability Matrix（Issue #2237 P0-5）

`SKILL.md` の "Reused Agents" 節から参照される enforcement contract の詳細。

| Role | Authority | 必須入力 | 本番制約 |
|---|---|---|---|
| runtime observer | interpreter | private runtime evidence + digest | no Agent/Skill/write、raw 出力禁止 |
| codebase investigator | advisory（current worktree を authority にしない） | `git show <base_sha>:<path>` 等でmaterialize した入力 | base_sha 非束縛の調査結果は finding authority にしない。substantive なcaller-supplied task にのみ `agy_advisory_native_fallback_allowed: true` + `authoritative_base_sha`を配線し、AGY operational failure 後の native fallback 結果を role adapter で`EvidenceBundle`/`OBSERVER_RESULT_V1` へ正規化する（native `failed`/`inconclusive`・`base_sha` 不一致はtyped failure、Issue #2374）。`run_retrospective.py`側も各`evidence_refs`エントリの`commit_sha`文字列一致だけで信用せず、`git show <base_sha>:<path>`を独立実行してバイト単位で`excerpt_sha256`を再計算・比較する（現在worktreeのbytesをauthorityにしない、PR #2387 review fix_delta P0-4） |
| web researcher | URL discovery / claim interpretation | bounded query | 最終 evidence は Web collector（`collect_web_source`）で再取得・digest 化してから finding authority にする |
| evaluator | privileged synthesis | validated projection のみ | Web/Bash/Agent/Skill/write 全禁止 |

## enforcement 手段

- runtime observer / evaluator: 専用 `.claude/agents/*.md` の `tools` frontmatter を空リスト（no tool）
  にし、`disallowedTools` に `Agent`/`Skill`/`Write`/`Edit`/`MultiEdit`/`NotebookEdit`/`Bash`/`WebFetch`/
  `WebSearch` を明示する。両者とも本 Issue の scope で新規実装され、leaf 制約はファイル自体で完結する。
- codebase investigator / web researcher: 既存 SubAgent（`.claude/agents/codebase-investigator.md` /
  `.claude/agents/web-researcher.md`）をそのまま再利用する。両者の advisory / URL-discovery 権限は
  既存ファイルの frontmatter・prose がすでに定義しており、本 Issue はこれを変更しない（再利用のみ）。
  advisory な調査結果を finding authority に格上げしないのは、`run_retrospective.py` の
  `build_finding_sets` / `run_evaluation` が observer wave の `EvidenceBundle`（strict schema
  validation 済み）のみを evaluator へ渡す構造そのものによって担保される。

## production Agent invocation 層の permission policy（本番呼び出し層の権限方針）

`run_retrospective.DelegatedAgentPermissionPolicy` が、委譲した Agent が試みる以下の操作を拒否する:

- `git commit`/`git push`
- `gh issue`/`gh pr`/`gh api`/`gh comment`/`gh release`
- filesystem write（`check_filesystem_write` は常に拒否）
- unapproved Bash command（allowlist 外はすべて拒否）
- 対象 run 以外の resume（`run_id` 不一致は拒否）

各 run の private temp artifact は `run_retrospective.run_scoped_temp_dir`（mode `0700`）が管理し、
success / exception / SIGINT / SIGTERM の全経路で削除する。
