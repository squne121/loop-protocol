---
title: Scope Rollup Policy
schema_version: 1
related_script: ../scripts/plan_issue_scope_rollup.py
related_issues: ["#316"]
status: active
---

# Scope Rollup Policy

このポリシーは `plan_issue_scope_rollup.py` の判定ロジックと、
`issue-refinement-loop` / `impl-review-loop` における統合アクションの選択基準を定義する。

## 目的

AI エージェントが同一ファイル・同一 skill family の修正を別 PR に分散させることを防ぐ。
orchestrator は `ISSUE_SCOPE_ROLLUP_PLAN_V2` を受け取り、このポリシーに従って統合可否を判断する。

---

## 1. 統合可能な条件（mergeable conditions）

以下のいずれかの条件を満たす場合、統合の検討対象となる。

| 条件 | シグナル名 |
|---|---|
| 同一 dedupe_key を持つ Issue / PR が存在する | `shared_dedupe_key` |
| 完全一致する Allowed Paths を持つ Issue / PR が存在する | `exact_allowed_path_overlap` |
| 同一 skill family（`.claude/skills/<family>/`）に属する変更である | `same_skill_family` |
| 同一 parent_issue を持つ Issue / PR が存在する | `same_parent_issue` |
| 同一 failure_mode_marker を持つ Issue / PR が存在する | `same_failure_mode_marker` |

---

## 2. 統合不可能な条件（non-mergeable conditions）

以下のいずれかに該当する場合は、統合してはならない。

1. **docs-only 変更と runtime 変更の混在**
   - `docs/` のみを変更する Issue と、`src/` / テスト / スクリプト等の runtime 変更 Issue は同一 PR に統合しない。
2. **security / auth / permission / sandbox 変更の混在**
   - セキュリティ・認証・権限・サンドボックスに関連する変更は、通常改善 Issue と同一 PR に統合しない。
   - `suggested_action: human_review_required` を必ず設定し、人間が判断する。
3. **Allowed Paths 超過**
   - 統合によって Allowed Paths の範囲を超える変更が生じる場合は統合しない。
4. **異なるアーキテクチャ層（`src/state` vs `src/render` 等）の混在**
   - CLAUDE.md の「不変のアーキテクチャ原則」で分離が要求されている層を跨ぐ変更は統合しない。
5. **closed / not_planned Issue との統合**
   - closed または not_planned になった Issue の変更を open Issue に取り込んではならない。

---

## 3. 統合アクション（5 種類）

`ISSUE_SCOPE_ROLLUP_PLAN_V2.candidates[].suggested_action` に設定される値。

### 3.1 `merge_into_current_pr`

同一 PR に統合する。
適用条件: `confidence: high` かつ `shared_dedupe_key` シグナルあり、または高い信頼度で同一変更セットと判断できる場合。

### 3.2 `amend_current_issue`

現在の Issue 本文（Allowed Paths / Outcome / AC / VC 等）を修正して対象を吸収する。
適用条件: `confidence: high` または `confidence: medium` で `exact_allowed_path_overlap` + `same_parent_issue` の組み合わせ。

### 3.3 `create_parent_rollup_issue`

複数の関連 Issue を束ねる parent rollup Issue を新規作成し、それぞれを child として管理する。
適用条件: `confidence: medium` で `same_parent_issue` シグナルあり、または同一 skill family の Issue が 3 件以上ある場合。

### 3.4 `keep_separate_with_reason`

統合せず別 Issue / PR として維持する。理由を `ISSUE_SCOPE_ROLLUP_DECISION_V2` に記録すること。
適用条件: `confidence: low`、または統合不可能な条件に該当する場合。

### 3.5 `human_review_required`

AI による自動判断を行わず、人間が統合可否を判断する。
適用条件: security / auth / permission / sandbox 関連、または `confidence` 判定が困難な複合シグナルの場合。

---

## 4. confidence_rules（信頼度ルール）

### high（高信頼度）

以下の条件のいずれかを満たす場合のみ `high` を設定する。

```yaml
high_conditions:
  - signals: [shared_dedupe_key]          # 同一 dedupe_key は最強シグナル
  - signals: [exact_allowed_path_overlap, same_parent_issue]
  - signals: [exact_allowed_path_overlap, same_failure_mode_marker]
```

**重要制約**: `same_skill_family` のみのシグナルは `high` を禁止する（後述の AC6 要件）。

### medium（中信頼度）

```yaml
medium_conditions:
  - signals: [exact_allowed_path_overlap]  # 単体でも medium
  - signals: [same_parent_issue]           # 単体でも medium
  - signals: [same_skill_family, <any_other_signal>]  # 追加シグナルがあれば medium
  - signals: [same_failure_mode_marker]    # 単体でも medium
```

### low（低信頼度）

```yaml
low_conditions:
  - signals: [same_skill_family]  # same_skill_family のみ -> low（high 禁止）
  - signals: []                   # シグナルなし（candidates に含まれない）
```

---

## 5. `same_skill_family` only は `high` 禁止（adversarial 防止ルール）

`same_skill_family` シグナル単体では `confidence: high` を設定してはならない。

**理由**: 同一 skill family に属するだけでは、変更内容の重複・競合を確定的に判断できない。
Allowed Paths の実際の重複や dedupe_key の一致がなければ、統合は安全ではない可能性がある。

```yaml
adversarial_rule:
  condition: signals == [same_skill_family]
  prohibited: confidence: high
  required: confidence: low
  rationale: "skill family 共有のみでは Allowed Paths 重複・dedupe_key 一致の証拠にならない"
```

---

## 6. `ISSUE_SCOPE_ROLLUP_DECISION_V2` 常時記録要件

統合を実施した場合・しなかった場合を問わず、orchestrator は必ず
`ISSUE_SCOPE_ROLLUP_DECISION_V2` を LOOP_STATE に記録しなければならない。

```yaml
ISSUE_SCOPE_ROLLUP_DECISION_V2:
  schema_version: 2
  recorded_at: "<ISO8601>"
  rollup_plan_ref:
    body_sha256: "<plan の body_sha256>"
    generated_at: "<plan の generated_at>"
  decision: executed | skipped | deferred | human_review_required
  executed_actions: []           # 実施したアクションのリスト（decision: executed の場合）
  skipped_reason: null           # decision: skipped の場合の理由
  candidates_reviewed:
    - kind: "issue|pr"
      number: <int>
      confidence: "high|medium|low"
      suggested_action: "<action>"
      final_decision: "accepted|rejected|deferred|human_review_required"
      rejection_reason: null      # final_decision: rejected の場合のみ設定
```

**記録タイミング**:
- `issue-refinement-loop`: Step 0d 完了後、Step 0-hygiene 実行前に記録する。
- `impl-review-loop`: Step 2.5 完了後、worktree 作成（Step 3）前に記録する。

**記録先**: `LOOP_STATE.scope_rollup_decision` フィールド（LOOP_STATE に追加）。

---

## 7. orchestrator の判断ルール

```yaml
orchestrator_rules:
  confidence_high:
    action: "候補ごとに suggested_action を LOOP_STATE に記録し、統合実施前に orchestrator が判断する"
    security_override: "security 関連は suggested_action を human_review_required に上書きする"
    auto_execute: false   # high でも自動実行しない。orchestrator が明示的に判断する

  confidence_medium:
    action: "LOOP_STATE に記録し、推奨アクションを提示する。自動実行はしない"
    auto_execute: false

  confidence_low:
    action: "LOOP_STATE に記録するが、アクション不要（keep_separate_with_reason）"
    auto_execute: false

  security_candidates:
    action: "human_review_required を設定して即時停止。AI による自動統合禁止"
    auto_execute: false
```

---

## Related

- `../scripts/plan_issue_scope_rollup.py` — このポリシーを実装するスクリプト
- `.claude/skills/issue-refinement-loop/SKILL.md` — Step 0d でこのポリシーを適用
- `.claude/skills/impl-review-loop/steps/preparation.md` — Step 2.5 でこのポリシーを適用
- `.claude/skills/impl-review-loop/steps/step-5-feedback-and-termination.md` — DECISION_V2 記録セクション
