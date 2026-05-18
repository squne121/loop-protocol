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
  unmatched_keywords: ["..."]              # マッチしなかったキーワード（SSOT 未整備の示唆）
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
| `partial` | 一部キーワードはマッチしたが、未マッチ（`unmatched_keywords` 非空）あり |
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
