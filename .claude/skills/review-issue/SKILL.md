---
name: review-issue
description: GitHub Issue 本文を `check_issue_contract.py` で決定論的にレビューし、`REVIEW_ISSUE_RESULT_V1` を返す script-first skill。VC の動作検証はしない（pr-review-judge / test-runner の責務）。「Issue ◯◯ レビュー」「review issue」のトリガーで使う。
---

# Review Issue

Issue 本文の構造品質を `.claude/skills/review-issue/scripts/check_issue_contract.py` で機械的に判定し、JSON 結果を `REVIEW_ISSUE_RESULT_V1` として返す。

判定ロジックは checker を SSOT とする。LLM は checker JSON を整形・転送するだけで、独自の自然言語判定で結果を補完しない。

## Input

- `issue_number`（必須）
- `invoked_as_loop`（任意、bool）: `issue-refinement-loop` から呼ばれた場合 `true`、人間直起動なら `false`

## Procedure

1. `gh issue view <番号> --json title,body,labels` で本文を取得する。
2. 本文を一時ファイルに保存し、`python3 .claude/skills/review-issue/scripts/check_issue_contract.py --file <tmp> --json` を実行する。
3. checker の JSON をそのまま `REVIEW_ISSUE_RESULT_V1` に整形する（`verdict` / `deterministic_checks` / `blocking_issues` / `non_blocking_improvements` / `diff_proposal` を保持）。
4. `verdict: needs-fix` の場合のみ `diff_proposal` を呼び出し元に提示する。本文書き戻しは Step 5 の条件分岐に従う。
5. 本文書き戻し条件:

| Verdict | invoked_as_loop | アクション |
|---|---|---|
| `approve` | * | レビュー結果のみ返して終了 |
| `needs-fix` | `true` | `diff_proposal` を返し、本文更新は呼び出し元（`issue-refinement-loop`）に委ねる。本 skill では `gh issue edit` しない |
| `needs-fix` | `false` | ユーザーに「この差分を Issue 本文に適用しますか？（yes/no）」と明示確認。承認時のみ `edit-issue` skill を呼ぶ |

## Checker contract (C1〜C12)

決定論的判定の詳細仕様は `scripts/check_issue_contract.py` に集約する。本 SKILL.md には判定表・regex・Step 4 自然言語評価を重複記載しない。checker は以下を返す:

- `C1_required_sections` 〜 `C12_product_trace_fields_structure` の 12 件の `pass | fail | warn | n/a | legacy_missing_applicability` 値
- `blocking_issues`: 各 fail の説明 string 配列
- `non_blocking_improvements`: 各 warning が `{code, severity, evidence, suggested_action}` の dict として配列
- `diff_proposal.add`: C1 fail 時の `missing_section_skeleton` 等、機械的に挿入可能な skeleton 配列
- `verdict`: いずれか fail があれば `needs-fix`、それ以外は `approve`

`C12_product_trace_fields_structure` は Product Spec / task-lineage Issue に限って適用され、`product_spec_id` / `requirement_id` / `source_task_id` の構造欠落・placeholder・形式不正を fail にする。非該当 Issue では `n/a` を返し verdict を変えない。

## Output (REVIEW_ISSUE_RESULT_V1)

```yaml
REVIEW_ISSUE_RESULT_V1:
  status: ok | failed
  generated_at: <ISO 8601>
  generated_by: review-issue
  issue_url: https://github.com/<owner>/<repo>/issues/<番号>
  verdict: approve | needs-fix
  deterministic_checks: <checker JSON deterministic_checks をそのまま>
  blocking_issues: <checker JSON blocking_issues をそのまま>
  non_blocking_improvements: <checker JSON non_blocking_improvements をそのまま>
  diff_proposal: <checker JSON diff_proposal をそのまま>
  update_applied: true | false
  comment_url: <変更経緯コメント URL、適用時のみ>
```

## Contract

- 判定ロジックは checker を SSOT とする
- LLM は checker 結果を補完・再判定・上書きしない
- SKILL.md には C1〜C12 の詳細実装条件（regex・閾値・パターン）を重複記載しない
- 本 SKILL.md と checker の出力 schema が乖離した場合は checker を正とする

## Guardrails

- VC を実装後の動作確認に使わない（baseline fail の構造を見るのみ。動作検証は `pr-review-judge` / `test-runner` の責務）
- 本文更新は `edit-issue` skill 経由で行い、本 skill から直接 `gh issue edit` しない
- `approve` 判定時は `invoked_as_loop` の値に関わらず本文更新へ進まない
- `needs-fix` + `invoked_as_loop: true` の場合は `diff_proposal` だけ返し、本文更新を呼び出し元に委ねる
- 人間の明示的承認なく本文を書き換えない

## Related

- `.claude/skills/review-issue/scripts/check_issue_contract.py` — 決定論的判定エンジン（C1〜C12 の SSOT）
- `.claude/skills/review-issue/tests/test_check_issue_contract.py` — C1〜C11 の fixture-driven test
- `.claude/skills/review-issue/tests/test_check_issue_contract_c12.py` — C12 / warning 群 / C1 skeleton の fixture-driven test
- `.claude/skills/issue-contract-review/SKILL.md` — 着手直前の preflight（本 skill の次段）
- `.claude/skills/edit-issue/SKILL.md` — `needs-fix` 結果を本文に反映する手順
- `.claude/skills/issue-refinement-loop/SKILL.md` — Issue 改善ループ（本 skill を中で呼ぶ）
- [`.claude/skills/create-issue/references/body-authoring.md`](../create-issue/references/body-authoring.md) — VC 作成 / Anchor Verification 等の共通ガイドライン
- `.github/ISSUE_TEMPLATE/implementation.yml` / `research.yml` / `parent.yml` — 必須セクションの SSOT

## 出力制約 (OUTPUT_BUDGET_V1)

`docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` の制約に従う。routing-critical な機械可読フィールドは削らず、人間向け説明・証跡・diff 再掲のみを削減する。
`REVIEW_ISSUE_RESULT_V1` の全フィールドは必ず含める（routing 必須フィールド）。
