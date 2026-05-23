---
name: review-issue
description: GitHub Issue 本文が AI Agent にとって作業に迷わない品質か（コンテクスト・ハーネスエンジニアリング観点）を、決定論的チェック + 軽量な構造評価で判定し修正差分提案を生成する skill。VC の動作検証はしない（それは pr-review-judge / test-runner の責務）。「Issue ◯◯ レビュー」「review issue」のトリガーで使う。
---

# Review Issue

Issue 本文を読んで以下を **決定論的に** 判定し、`approve` / `needs-fix` の verdict と修正差分提案を返す。

## Input

- `issue_number`（必須）
- `invoked_as_loop`（任意、bool）: `issue-refinement-loop` から呼ばれた場合 `true`、人間直起動なら `false`

## Procedure

### 1. Issue 本文と種別を取得

```bash
gh issue view <番号> --json title,body,labels --jq '.title + "\n---LABELS---\n" + (.labels | map(.name) | join(",")) + "\n---BODY---\n" + .body'
```

Issue 種別の判定は **テンプレート SSOT に委ねる**:
- title prefix（`実装:` / `調査:` / `導入:`）
- labels（`phase/implementation` / `phase/research` / `tracking`）
- いずれかから `.github/ISSUE_TEMPLATE/{種別}.yml` を選び、その `body[].attributes.label` を必須セクション一覧として取得する

### 2. state ラベルによる事前判定

`state/needs-human` が付いている Issue は人間判断待ちで AI 着手不可。本 skill では構造チェックを軽量に行い、本文の品質詳細評価はスキップする（人間判断後に本文更新→再レビュー想定）。

### 3. 決定論的チェック（Blocking）

各チェックを `grep` / `awk` / 数値比較で機械判定する。1 つでも fail なら verdict は `needs-fix`。

| ID | チェック | 機械判定 |
|---|---|---|
| C1 | 必須セクション存在 | `grep -qF "## <label>"` を ISSUE_TEMPLATE 由来の各 label に対して |
| C2 | Stop Conditions 6 項目（implementation のみ） | `## Stop Conditions` 配下の `^- ` 行数 ≥ 6 |
| C3 | AC が `- [ ]` 形式 | `## Acceptance Criteria` 配下に `^- \[.\]` 行が 1 件以上 |
| C4 | VC コマンド存在 | `## Verification Commands` 配下に `^[\-\$]` 始まり行が 1 件以上 |
| C5 | AC ⇔ VC 番号一致 | `^- \[.\] AC[0-9]+` 件数 = `# AC[0-9]+` 件数 |
| C6 | 主観表現の混入 | AC / VC 本文に「適切に動作」「品質を改善」「最適化」等が **含まれない** |
| C7 | Required Skills 意味論 | ワークフロー skill（`implement-issue` / `pr-review-judge` / `ssot-discovery` 等）/ document path（`docs/...` / `.md` / `/`）を **含まない** |
| C8 | Outcome 抽象パターン除外 | `## Outcome` 配下に「〜が決定される」「〜を検討する」「〜を改善する」等の動作状態のみ表現が **含まれない** |
| C9 | 適用判定不在 | `## Runtime Verification Applicability` セクションが存在しない。**implementation Issue は blocker**（`status: needs-fix`）。ただしスキーマ導入（#77）以前に作成された legacy Issue は `legacy_missing_applicability` 状態として扱い、着手前に人間確認を仰ぐことを推奨（blocker 猶予あり）。research / tracking Issue は warning（approve を妨げない） |
| C10 | deferred の検証先不明 | `decision: deferred` が宣言されているが、`deferred_destination`（destination_type + destination_ref）または `deferred_verification_condition` が **1 つでも欠けている**（blocker）。自由記述のみで半構造化フォーマットが未使用の場合も blocker |
| C11 | decision と runtime-verification タグの整合 | `decision: immediate` なのに AC に `<!-- runtime-verification: true -->` タグが 1 つもない（blocker）、または `decision: not_applicable` / `deferred` なのに `<!-- runtime-verification: true -->` タグが存在する（矛盾 blocker） |

### 4. 軽量構造評価（non-blocking improvement 候補）

機械判定だけでは捕まらない構造的観点。**情報提示のみ** で blocking しない。

- **PR スコープのまとまり**: Allowed Paths が 1 つの Outcome のためだけに必要かを目視確認。例外：別レイヤーに **接続する** 要素を変更する場合（API 境界変更等）は、文脈整合を保つため 1 PR にまとめるのが妥当。レイヤー独立に分けると Issue 間で文脈が分断される
- **類似 Issue 重複**: `gh issue list --search "<keyword>" --state open` で OPEN Issue を列挙し、同一・類似 Outcome 候補があれば提示

### 5. Verdict 決定

- `approve`: C1〜C11 すべて pass（research / tracking Issue の C9 warn は approve を妨げない）
- `needs-fix`: C1〜C11 のいずれかが fail（implementation Issue の C9 fail / C11 fail を含む）
- `legacy_missing_applicability`: implementation Issue で C9 fail かつ legacy Issue（スキーマ導入 #77 以前に作成）の場合は `needs-fix` にしつつ、修正提案コメントに「legacy Issue のため blocker 猶予あり、着手前に適用判定セクションを追加することを推奨」と付記する

### 6. 差分提案生成

`needs-fix` の場合のみ。`追加すべき文` / `削除すべき文` / `書き換え案` の形式で具体的に示す（抽象論で終わらせない）。

### 7. 本文書き戻し（条件分岐）

| Verdict | invoked_as_loop | アクション |
|---|---|---|
| `approve` | * | レビュー結果のみ返して終了 |
| `needs-fix` | `true` | 差分提案を返し、本文更新は呼び出し元（`issue-refinement-loop`）に委ねる。本 skill では `gh issue edit` しない |
| `needs-fix` | `false` | ユーザーに「この差分を Issue 本文に適用しますか？（yes/no）」と明示確認。承認時のみ `edit-issue` skill を呼ぶ |

## Output (REVIEW_ISSUE_RESULT_V1)

```yaml
REVIEW_ISSUE_RESULT_V1:
  status: ok | failed
  generated_at: <ISO 8601>
  generated_by: review-issue
  issue_url: https://github.com/<owner>/<repo>/issues/<番号>
  verdict: approve | needs-fix
  deterministic_checks:
    C1_required_sections: pass | fail | n/a
    C2_stop_conditions_6: pass | fail | n/a
    C3_ac_checkbox_format: pass | fail | n/a
    C4_vc_commands_present: pass | fail | n/a
    C5_ac_vc_number_alignment: pass | fail | n/a
    C6_no_subjective_phrasing: pass | fail | n/a
    C7_required_skills_semantics: pass | fail | n/a
    C8_outcome_concreteness: pass | fail | n/a
    C9_runtime_applicability_present: pass | fail | warn | legacy_missing_applicability | n/a
    C10_deferred_destination_present: pass | fail | n/a
    C11_decision_tag_consistency: pass | fail | n/a
  blocking_issues: []
  non_blocking_improvements: []
  diff_proposal:
    add: []
    remove: []
    rewrite: []
  update_applied: true | false
  comment_url: <変更経緯コメント URL、適用時のみ>
```

## Guardrails

- **VC を実装後の動作確認に使わない**（baseline fail の構造を見るのみ。動作検証は `pr-review-judge` / `test-runner` の責務）
- 本文更新は `edit-issue` skill 経由で行い、本 skill から直接 `gh issue edit` しない
- `approve` 判定時は `invoked_as_loop` の値に関わらず本文更新へ進まない
- `needs-fix` + `invoked_as_loop: true` の場合は差分提案だけ返し、本文更新を呼び出し元に委ねる
- 人間の明示的承認なく本文を書き換えない

## Related

- `.claude/skills/issue-contract-review/SKILL.md` — 着手直前の preflight（本 skill の次段）
- `.claude/skills/edit-issue/SKILL.md` — `needs-fix` 結果を本文に反映する手順
- `.claude/skills/issue-refinement-loop/SKILL.md` — Issue 改善ループ（本 skill を中で呼ぶ）
- [`.claude/skills/create-issue/references/body-authoring.md`](../create-issue/references/body-authoring.md) — VC 作成 / Anchor Verification 等の共通ガイドライン
- `.github/ISSUE_TEMPLATE/implementation.yml` / `research.yml` / `parent.yml` — 必須セクションの SSOT

## 出力制約 (OUTPUT_BUDGET_V1)

`docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` の制約に従う。routing-critical な機械可読フィールドは削らず、人間向け説明・証跡・diff 再掲のみを削減する。
`REVIEW_ISSUE_RESULT_V1` の全フィールドは必ず含める（routing 必須フィールド）。
