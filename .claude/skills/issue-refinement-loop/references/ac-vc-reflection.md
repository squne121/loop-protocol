# AC / VC Reflection

## Purpose

review 段階で本文品質を評価するとき、baseline 未実装状態と実装後 gate を取り違えないための reference。

## Reflection Rules (SubAgent-owned)

AC/VC の baseline fail 判定、reflection guard、および rewrite 時の期待動作に関する詳細は、`.claude/agents/issue-author.md` の **AC/VC Reflection & Rewrite Logic (SubAgent-owned)** セクションを参照すること。

orchestrator はこれらの判定ロジックを再実装せず、SubAgent 側の自律的判断に委譲する。

## Rewrite guard (orchestrator layer)

- `reviewer_feedback_text` は opaque forwarding payload として扱う。
- anchor comment が絡む場合も、raw snapshot ではなく正規化済み `anchor_comment_feedback` だけを `issue-author` へ渡す。

## FAIL_CLOSED_REWRITE_CONSTRAINTS_V1 Forwarding

`fail_closed.required == true` の場合、orchestrator は `fail_closed.rewrite_constraints`（`FAIL_CLOSED_REWRITE_CONSTRAINTS_V1` スキーマ）を `issue-author` への rewrite 入力に含める必要がある。

- `issue-author` は `required_sections` / `required_contract_keys` / `rewrite_constraints` フィールドを受け取り、不足セクション・不足契約キーの補完を優先する
- `rewrite_constraints.freeform_rewrite_forbidden == true` の場合、`issue-author` は自由文形式の改変を拒否する
- `human_decision_reframe` による override が許可されていても、`FAIL_CLOSED_REWRITE_CONSTRAINTS_V1` を無視してよいわけではない

### Rewrite 後の自動再検証（2 段構成）

`issue-author` による rewrite 完了後に以下の検証を順に実施する:

1. **pre-mutation dry-run checker**: mutation 前に新本文の静的検証（`check_issue_contract.py` 相当）を dry-run で実施する
2. **post-mutation fresh checker**: `gh issue edit` 実行後に GitHub から本文を再取得し、静的検証を再実施する

`post-mutation fresh checker` が exit 0 以外の場合、Review に進まず Rewrite を継続する。

### max_rewrite_attempts / no-progress detection

- `FAIL_CLOSED_REWRITE_CONSTRAINTS_V1.max_rewrite_attempts: 2` — この回数を超えて同一 fail_closed を解消できない場合は `human_judgment_required` へ遷移する
- **no-progress detection**: 連続する 2 rewrite で `checked_body_sha256` が変化しない場合、前進がないと判定し `human_judgment_required` を返す

## Must not

- verification owner が異なる VC を refinement loop 側で再分類しない
- SubAgent 側の reflection 判定を orchestrator 側で再解釈しない
- `human_decision_reframe` を validation bypass として扱わない（`FAIL_CLOSED_REWRITE_CONSTRAINTS_V1` は必ず転送する）
- `never_override_reason_codes`（`unknown_issue_kind` / `issue_kind_policy_load_error` / `contract_schema_parse_error` / `template_resolution_error` / `checker_internal_error`）に対して override を許可しない
