## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "#391"
goal_ref: "issue-refinement-loop/SKILL.md を薄い entrypoint に縮小し、長文 procedure を references/ に分割する"
change_kind: workflow
```

## Parent Issue

#391

## Parent Goal Ref

- Goal: issue-refinement-loop を script-first / thin entrypoint に再構成し、固定 token cost を削減する
- Desired Destination: SKILL.md を 500 行以下に縮小し、詳細 procedure は `references/` に progressive disclosure 構成で分割。判定ロジックは Phase 1 の planner script を SSOT として consume する。

## Current Validated Scope

- `.claude/skills/issue-refinement-loop/SKILL.md` の thin entrypoint 化
- `.claude/skills/issue-refinement-loop/references/` 配下への詳細 procedure 移設
- `references/index.md` による progressive disclosure 表の整備
- planner 判定条件の prose 重複を SKILL.md から除去
- `disable-model-invocation: true` / `paths` frontmatter 採否の明示決定

## Remaining Parent Gaps

- [ ] #394: SubAgent-owned contract migration（web-researcher の retry/fallback/attempt log 等の `.claude/agents/*.md` 側責務整理）
- [ ] #391 Phase 4: 残り childen が定義された場合の対応

## Required Skills

- Markdown / YAML フロントマター編集
- Claude Code skill / subagent 構造（frontmatter フィールド・progressive disclosure）の理解
- pytest によるテキスト fixture / forbidden-fragment 検証
- `rg` / `wc` / `gh` CLI による静的検証

## Depends On

- Phase 1 として #392（planner 抽出）の PR #407 が main へ merge 済み（前提条件）
- 軽量化前提として #295 が CLOSED COMPLETED 済み（前提条件）
- 上記はすべて満たされているため、ブロッカーなし

## Background

- Phase 1 で deterministic planner/checker を導入した後、SKILL.md 本文から長文 procedure / guard 詳細 / schema 重複を削減する。
- 目的は単なるファイル分割ではなく、スキル起動時に毎回読む本文を薄くし、必要な詳細だけを `references/` から読む progressive disclosure 構成にすること。

## Baseline

- base_ref: main after PR #407 merge（2026-05-26 時点測定）
- baseline 測定値:
  - `wc -l .claude/skills/issue-refinement-loop/SKILL.md` = 1280 行
  - `wc -c .claude/skills/issue-refinement-loop/SKILL.md` = 69901 bytes
  - frontmatter description chars / when_to_use chars / references/index.md chars / references file count は PR 本文で実測値を記録する
- 比較対象は #295 Issue 当時の 1226 行 baseline ではなく、本 baseline に固定する。

## Outcome

- `issue-refinement-loop/SKILL.md` は entrypoint / Inputs / LOOP_STATE summary / planner 呼び出し / SubAgent routing / Stop Conditions のみを保持する。
- 詳細 procedure は references に分割される。

## In Scope

- `references/` の新規作成または再編
- SKILL.md から以下を references へ移動:
  - anchor comment handling
  - scope signal guard
  - AC/VC reflection confirmation
  - follow-up materialization
  - `WEB_RESEARCH_RESULT_V1` の orchestrator-side routing を references に移す（retry/fallback/attempt log の詳細は link-only とし、#394 または worker/wrapper 側へ委譲）
  - loop termination table
- SKILL.md の行数・bytes を baseline と比較して PR 本文に記録
- Phase 1 planner の JSON contract を正本として参照
- 既存 VC の更新
- `references/index.md` を作成し、topic / file / loaded_when / owner / moved_from / must_not の表で progressive disclosure 構造を可視化する
- `disable-model-invocation: true` の採否を明示的に決定し PR 本文に記録する
- `paths` frontmatter の適用可否を明示的に決定し PR 本文に記録する
- planner 判定条件の prose 重複（現 Step 1 等）を SKILL.md から除去する

## Out of Scope

- planner script の新規判定追加
- SubAgent prompt の責務移動
- OPEN Issue の close 判断
- #389 runtime 計測の実施
- web-researcher / gemini-cli-headless-delegation の retry / fallback / attempt log 設計変更（#394 または worker/wrapper の責務）
- orchestrator 側で Gemini retry_count / fallback_query / raw_grounding_state を保持すること
- `.claude/agents/*.md` の変更（SubAgent-owned contract migration は #394 の責務）
- SubAgent frontmatter / tools / disallowedTools / skills / model / retry / fallback の変更

## Acceptance Criteria

- [ ] **AC1**: `.claude/skills/issue-refinement-loop/references/index.md` が存在する（references/ ディレクトリ自体は #392 で既存のため、新規 index.md の存在を baseline 失敗の signal とする）
- [ ] **AC2**: SKILL.md が thin entrypoint であることを示す sentinel marker がある
- [ ] **AC3**: `.claude/skills/issue-refinement-loop/SKILL.md` の行数が 500 行以下である。500 行を超える場合は PR 本文に以下を表形式で記録する:
  - 残す section 名
  - references に移せない理由
  - 次 follow-up Issue 番号
  - SKILL.md lines / bytes
  - frontmatter description chars
  - when_to_use chars（存在する場合）
  - references/index.md chars
  - references file count
  単なる baseline 比減少は pass 条件にしない。VC は `test "$(wc -l < ...)" -le 500` で機械的に判定する。500 超過時の PR 本文記録要件は維持。
- [ ] **AC4**: references に分割された各 topic が SKILL.md からリンクされている
- [ ] **AC5**: `REFINEMENT_LOOP_PLAN_V1` を consume する記述が残っている
- [ ] **AC6**: 既存 termination / follow-up / web research / anchor handling の意味論が失われていない
- [ ] **AC7**: `pnpm` / `pytest` gate が PASS する
- [ ] **AC8**: SKILL.md は `REFINEMENT_LOOP_PLAN_V1` を consume するだけで、`investigation_policy` / `web_research_policy` / `scope_signal_guard` の判定条件を prose で再定義しない。
- [ ] **AC9**: `.claude/skills/issue-refinement-loop/tests/test_thin_entrypoint.py` に forbidden-fragment test があり、SKILL.md 内に planner 判定条件の prose 再実装が残っていないことを CI で検証する。
- [ ] **AC10**: `references/index.md` が存在し、topic / file / loaded_when / owner / moved_from / must_not 列を持つ表で全 references を一覧化している。
- [ ] **AC11**: `disable-model-invocation: true` の採否と `paths` frontmatter の適用可否が PR 本文で明示決定されている。PR 本文の Frontmatter Decision セクションで実測値を記録（baseline_vc_preflight 対象外、pr-review-judge / 人間レビューで確認）。

## Verification Commands

```bash
# AC1: 新規 references/index.md ファイルの存在を baseline で fail させる
$ test -f .claude/skills/issue-refinement-loop/references/index.md  # AC1

# AC2 / AC5: thin entrypoint sentinel + planner consume の明示
$ rg -n "thin entrypoint|判定ロジックは planner を SSOT|REFINEMENT_LOOP_PLAN_V1" .claude/skills/issue-refinement-loop/SKILL.md  # AC2  # AC5

# AC3: SKILL.md 行数が 500 以下であること（baseline 1280 行 → fail を担保）
$ test "$(wc -l < .claude/skills/issue-refinement-loop/SKILL.md)" -le 500  # AC3

# AC4: 分割対象 topic ごとの references ファイル参照リンクが SKILL.md に存在
$ rg -n "references/anchor-comment-handling\.md|references/web-research-routing\.md|references/follow-up-materialization\.md|references/termination-policy\.md|references/ac-vc-reflection\.md|references/scope-signal-guard\.md" .claude/skills/issue-refinement-loop/SKILL.md  # AC4

# AC6: 主要 topic 別ファイルが references/ に存在することを個別に検証
$ test -f .claude/skills/issue-refinement-loop/references/anchor-comment-handling.md  # AC6
$ test -f .claude/skills/issue-refinement-loop/references/web-research-routing.md  # AC6
$ test -f .claude/skills/issue-refinement-loop/references/follow-up-materialization.md  # AC6
$ test -f .claude/skills/issue-refinement-loop/references/termination-policy.md  # AC6
$ test -f .claude/skills/issue-refinement-loop/references/ac-vc-reflection.md  # AC6
$ test -f .claude/skills/issue-refinement-loop/references/scope-signal-guard.md  # AC6

# AC7: 必須検証コマンド（compound を回避するため 1 行 1 コマンドに分割）
$ pnpm typecheck  # AC7
$ pnpm lint  # AC7
$ pnpm test  # AC7
$ pnpm build  # AC7
$ uv run pytest .claude/skills/issue-refinement-loop/tests/ -v  # AC7

# AC8: planner 判定条件の prose 再実装が SKILL.md に残っていないこと（明示 wrap で baseline fail を担保）
$ if rg -nq "codebase_required\s*[:=]\s*true|web_research_policy\.required\s*[:=]\s*true" .claude/skills/issue-refinement-loop/SKILL.md; then exit 1; else exit 0; fi  # AC8

# AC9: forbidden-fragment test の存在
$ test -f .claude/skills/issue-refinement-loop/tests/test_thin_entrypoint.py  # AC9
$ uv run pytest .claude/skills/issue-refinement-loop/tests/test_thin_entrypoint.py -v  # AC9

# AC10: references/index.md が topic / file / loaded_when / owner / moved_from / must_not 列の表を持つ
$ rg -nq "\| topic \| file \| loaded_when \| owner \| moved_from \| must_not \|" .claude/skills/issue-refinement-loop/references/index.md  # AC10
```

## Allowed Paths

- `.claude/skills/issue-refinement-loop/SKILL.md`
- `.claude/skills/issue-refinement-loop/references/`
- `.claude/skills/issue-refinement-loop/tests/`
- `.claude/skills/issue-refinement-loop/fixtures/`

## Stop Conditions

- Phase 1 planner の schema 変更が必要になった場合
- SubAgent prompt の責務移動が必要になった場合
- references 分割だけでは意味論が保持できない場合
- Allowed Paths 外の変更が必要になった場合
- nested SubAgent delegation が必要になった場合
- 外部サービス利用・権限昇格・既存テスト大規模改変が必要になった場合
- `.claude/agents/*.md` の変更が必要になった場合 → #394 に defer
- SubAgent frontmatter / tools / disallowedTools / skills / model / retry / fallback の変更が必要になった場合 → #394 または別 Issue
- planner script（plan_refinement_loop.py）の判定ロジック変更が必要になった場合 → #392 follow-up Issue

## Runtime Verification Applicability

decision: not_applicable
reason: skill documentation / reference structure change。静的検証と tests で確認する。

## Delivery Rule

- `1 Issue = 1 PR`
- Draft PR 既定
- Phase 1 merge 後に着手

## References

- 親 Issue: #391（perf: issue-refinement-loop を script-first / thin entrypoint へ）
- 兄弟 Issue: #392（planner 抽出、PR #407 で merge 済み）、#394（SubAgent-owned contract migration）
- 先例: #264 / PR #311（gemini-cli-headless-delegation thin entrypoint 化、disable-model-invocation: true 採用、SKILL.md 大幅縮小）
- 関連クローズ済み: #203（web-researcher retry は not_planned）
- 一次情報: Claude Code Skills docs（https://code.claude.com/docs/en/skills）、Subagents docs（https://code.claude.com/docs/en/sub-agents）
- anchor comment: https://github.com/squne121/loop-protocol/issues/393#issuecomment-4537929910

