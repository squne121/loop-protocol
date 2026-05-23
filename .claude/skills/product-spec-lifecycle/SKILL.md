---
name: product-spec-lifecycle
description: docs/product/** の作成・更新・archive・tasks.md GitHub Issue 変換の手順 skill。path-scoped rule `.claude/rules/product-spec-lifecycle.md` から呼び出される。
---

# Product Spec Lifecycle Skill

`docs/product/**` の操作手順を定義する。
このファイルは作業入口として、操作種別に応じた手順を提供する。

lifecycle 正本（状態遷移・token_policy・EARS など詳細定義）は
`docs/dev/product-spec-lifecycle.md` を参照する。

## Input

- `operation`: `create` | `update` | `archive` | `tasks_to_issues`
- `target_path`: 対象の `docs/product/**` パス
- `linked_issue`: 変更に対応する GitHub Issue 番号

## Procedure

### Create（新規 spec 作成）

1. **frontmatter 確認**
   ```yaml
   status: draft
   issue: "#<N>"
   parent_issue: "#<N>"   # parent tracker がある場合
   ```
2. **compact spec 形式**（250 行以内）で作成する。
   - 必須フィールド: `intent`, `requirement_id`, `acceptance_criteria`, `non_goals`, `trace_links`
   - EARS 記法で acceptance criteria を書く
   - 背景説明・重複散文・長い総論は禁止
3. **registry 更新**: `docs/dev/ssot-registry.md` に entry + directory mapping を同一 PR で追加する。
4. **routing 更新**: `docs/dev/workflow.md` SSOT Routing Table にエントリを追加する。
5. **CLAUDE.md / rules**: path-scoped rule（`.claude/rules/product-spec-lifecycle.md`）は既に適用済み。
   個別のローカル CLAUDE.md が必要な場合のみ `docs/product/CLAUDE.md` を参照する。
6. **GitHub Issue 開設**: `1 Issue = 1 PR` を遵守し、追加ドキュメントは対応 Issue を持つ。

```bash
# 行数ガード（250 行以内）
test "$(wc -l < "$TARGET_PATH")" -le 250 && echo "PASS: line count" || echo "FAIL: line count exceeded"
```

### Update（差分更新）

1. **diff-first** で変更する。影響する `REQ-xxx` / section のみ変更する。
   ```yaml
   full_regeneration: prohibited  # 全文再生成しない
   ```
2. frontmatter の `issue` フィールドを今回の PR linked issue に更新する。
3. 変更が prior acceptance criteria を無効化する場合は **spec delta** として扱う:
   - 先に spec delta issue を起票してから実装 issue を起票する。
   - spec delta なしに直接実装 issue を起票しない。
4. `docs/dev/ssot-registry.md` のエントリが引き続き正確かを確認する（変更不要なら確認のみ）。

```bash
# 全文再生成検出ガード
! rg -n "canonical_source:.*(specify|openspec)|generated_artifacts:.*normative" \
    docs/adr docs/product 2>/dev/null && echo "PASS: no full_regen marker" || echo "FAIL: full_regen marker detected"
```

### Archive（アーカイブ）

1. frontmatter を更新する:
   ```yaml
   status: archived  # または superseded
   superseded_by: "#<N> / docs/product/<successor>.md"  # status: superseded のとき必須
   archived_reason: "<reason>"
   archived_date: "YYYY-MM-DD"
   ```
2. **ファイル削除禁止** — archived 状態へ遷移させる（traceability 保全のため）。
3. `docs/dev/ssot-registry.md` のエントリを `status: archived` に更新する。
4. 参照元（`CLAUDE.md`, `project-constitution.md`, `workflow.md`）の conditional reference を更新する。

### Tasks.md Materialization（GitHub Issue 変換）

`tasks.md` は **staging artifact**（一時的な分解用ファイル）。GitHub Issues が tracking SSOT。

1. `tasks.md` の各タスクに対して trace fields を確認する:
   - `product_spec_id`（例: `game-thesis`）
   - `requirement_id`（例: `REQ-001`）
   - `source_task_id`（例: `TASK-003`）
   - `parent_issue`（delivery-rollup 親 Issue 番号）
2. **speckit-taskstoissues** または `gh issue create` で GitHub Issue を起票する。
3. Issue 起票後、`tasks.md` を **derived artifact として降格**する（tracking に使わない）。
4. 起票 Issue を delivery-rollup parent の本文に記録する（`closure_mode: child-complete` を遵守）。

```bash
# speckit-taskstoissues を使う場合
# .claude/skills/speckit-taskstoissues/SKILL.md を参照する

# ネイティブ issue 起票を使う場合
# .claude/skills/create-issue/SKILL.md を参照する
```

**禁止パス**:
- `tasks.md` から直接実装しない（GitHub Issue materialization を経由すること）
- materialization 後に `tasks.md` を tracking SSOT として使わない
- 再生成した `tasks.md` が既存 GitHub Issues と乖離する場合は停止して人間判断を仰ぐ

## speckit-* / create-issue / impl-review-loop との接続方針

| ツール | 接続タイミング | 参照先 |
|---|---|---|
| `speckit-analyze` / `speckit-specify` | spec 起草フェーズ（draft 段階） | `.claude/skills/speckit-*/SKILL.md` |
| `speckit-tasks` | spec → tasks.md 生成 | `.claude/skills/speckit-tasks/SKILL.md` |
| `speckit-taskstoissues` | tasks.md → GitHub Issue 変換 | `.claude/skills/speckit-taskstoissues/SKILL.md` |
| `create-issue` | ネイティブ Issue 起票（speckit なし） | `.claude/skills/create-issue/SKILL.md` |
| `issue-contract-review` | Issue → go/no-go 判定 | `.claude/skills/issue-contract-review/SKILL.md` |
| `impl-review-loop` | go 判定後の実装ループ | `.claude/skills/impl-review-loop/SKILL.md` |

**speckit-* との接続方針**（`docs/adr/0002-sdd-tool-adoption.md` §"Spec Kit 採用" より）:

- Spec Kit は `docs/` canonical に対して **upstream-compatible adoption** で使う。
- `.specify/` は derived workbench。`docs/product/**` の SSOT 性は維持する。
- `speckit-*` skills が出力した artifact と `docs/product/**` が矛盾する場合は **`docs/product/**` が勝つ**。
- `speckit-taskstoissues` は `tasks.md` の GitHub Issue 変換を補助するが、起票後の tracking は Issues/PRs に移る。

## Output (PRODUCT_SPEC_LIFECYCLE_CHECK_V1)

操作完了時に以下の機械可読 YAML を出力する:

```yaml
PRODUCT_SPEC_LIFECYCLE_CHECK_V1:
  status: ok | failed | blocked
  generated_at: <ISO 8601>
  generated_by: product-spec-lifecycle
  operation: create | update | archive | tasks_to_issues
  target_path: docs/product/<filename>.md
  linked_issue: "#<N>"
  checks:
    frontmatter_valid: pass | fail
    compact_spec_line_count: pass | fail | not_applicable
    full_regen_guard: pass | fail
    ssot_registry_updated: pass | fail | not_applicable
    workflow_routing_updated: pass | fail | not_applicable
    archive_fields_present: pass | fail | not_applicable
    tasks_trace_complete: pass | fail | not_applicable
  warnings: []
  errors: []
```

`status: ok` は全 checks が `pass` または `not_applicable` の場合のみ返す。

## Guardrails

- `full_regeneration: prohibited` — spec 全文再生成は Stop Condition
- `delete_file: prohibited` — archive 時でもファイル削除禁止
- `tasks.md` から直接実装しない（materialization 経由必須）
- `.specify/` 由来 artifact を SSOT として扱わない
- Allowed Paths 外の編集は行わない

## Related

- `docs/dev/product-spec-lifecycle.md` — lifecycle 正本（状態遷移・EARS・token_policy）
- `.claude/rules/product-spec-lifecycle.md` — path-scoped rule（自動適用トリガー）
- `docs/adr/0002-sdd-tool-adoption.md` — SDD ツール採否・generated_artifact_boundary
- `docs/dev/ssot-registry.md` — SSOT カタログ
- `docs/dev/workflow.md` — repo-wide execution workflow
- `.claude/skills/speckit-taskstoissues/SKILL.md` — tasks.md → Issue 変換
- `.claude/skills/create-issue/SKILL.md` — ネイティブ Issue 起票
- `.claude/skills/impl-review-loop/SKILL.md` — 実装ループ
