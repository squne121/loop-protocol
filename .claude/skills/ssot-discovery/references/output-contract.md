# SSOT_DISCOVERY_RESULT_V1 — 出力契約

## スキーマ

```yaml
SSOT_DISCOVERY_RESULT_V1:
  status: ok | partial | failed
  generated_at: "2026-05-18T12:34:56Z"   # ISO 8601 UTC
  generated_by: "ssot-discovery"
  inputs:
    task_keywords: ["..."]
    target_paths: ["..."]
    issue_number: 42                       # 任意
  matched_documents:
    - path: "docs/dev/workflow.md"
      relevance: "high"                    # high | medium | low
      reason: "worktree 運用の正本"
      sections:
        - "## Worktree 配置"
        - "## マージ後クリーンアップ"
    - path: "docs/adr/0001-architecture-baseline.md"
      relevance: "medium"
      reason: "src/state ↔ src/render 分離原則"
      sections: []
  # v1 optional extension: bounded section evidence. Old consumers MUST ignore
  # this unknown field and continue to consume matched_documents.
  section_limited_matches:
    - path: "docs/dev/workflow.md"
      source_commit: "<40-char git commit SHA>"
      blob_sha: "<git blob SHA>"
      content_sha256: "sha256:<hex>"
      heading: "Scope Collision Preflight（スコープ衝突の事前確認）"
      heading_level: 3
      start_line: 1                       # one-indexed, inclusive
      end_line_exclusive: 20              # one-indexed, exclusive
      permalink: "https://github.com/owner/repo/blob/<commit>/docs/dev/workflow.md#L1-L19"
      selector_version: "ssot-section-selector/v1"
      selection_reason_code: "heading_keyword_match"
      char_count: 100
      char_budget: 4000
  section_selection_outcomes:
    - path: "docs/dev/workflow.md"
      reason_code: "selected | section_not_found | section_budget_exceeded | document_not_found"
  unmatched_keywords: ["..."]              # マッチしなかったキーワード（SSOT 未整備の示唆）
  unmatched_paths: ["src/data/foo.ts"]    # マッチしなかった target_paths
  notes: []                                # 補足
  warnings: []
  errors: []                               # status=failed 時のみ必須
```

## relevance の判定基準

| relevance | 条件 |
|---|---|
| `high` | キーワードがファイル名 / `# 見出し` / `## 見出し` に直接含まれる |
| `medium` | キーワードが本文に出現する |
| `low` | カタログのマッピング由来（直接マッチなし、推定関連） |

## status の判定基準

| status | 条件 |
|---|---|
| `ok` | すべてのキーワード / パスについて 1 件以上のマッチを得た |
| `partial` | 一部キーワードはマッチしたが未マッチ（`unmatched_keywords` 非空）あり、またはパス入力でマッチしないパスが存在（`unmatched_paths` 非空） |
| `failed` | 入力不正・スクリプト実行失敗・SSOT カタログ不在 等 |

## 共通フィールド

- `generated_at`: ISO 8601 UTC タイムスタンプ
- `generated_by`: `"ssot-discovery"` 固定
- `inputs`: 受け取った入力を全て記録（呼び出し履歴の再現性）
- `warnings`: 致命的でないが注意すべき事項
- `errors`: `status: failed` 時に必須。問題と推奨次アクションを記載

## パース時の注意

- メインセッション / orchestrator は `matched_documents` を高 relevance 順に処理する
- `unmatched_keywords` は無視せず、SSOT 未整備として human review に回す候補にする
- 詳細出力（散文ログ）は本ペイロード外に出さない
- `section_limited_matches` と `section_selection_outcomes` は v1 の optional field である。旧 consumer は未知 field を無視し、既存の `matched_documents` 契約を継続利用する。
- consumer inventory: `ssot-discovery` を呼び出す Skill / orchestrator は、この optional field を消費する場合にのみ `selector_version` と reason code を確認する。未対応 consumer の migration は不要であり、互換性は unknown field ignore により維持される。
- 文書がマッチしても section が選べない場合、全体 status は既存の keyword/path match 規則を維持し、`section_selection_outcomes` の reason code を consumer が fail-closed routing に用いる。全文 fallback は禁止する。
