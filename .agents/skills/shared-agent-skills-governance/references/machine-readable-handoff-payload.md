# Machine-Readable Handoff Payload

SubAgent が返す構造化ペイロードの仕様。

## 1. 推奨フォーマット

YAML をデフォルトとする（JSON も可）。SubAgent は本文中にコードフェンスで囲んで返す：

````
```yaml
CONTRACT_NAME_V1:
  field_a: value
  field_b: value
```
````

## 2. 共通フィールド

すべての contract で以下を含める：

```yaml
<CONTRACT_NAME>:
  status: ok | failed | partial
  generated_at: 2026-05-18T12:34:56Z   # ISO 8601 UTC
  generated_by: <subagent-name>
  inputs:
    - <input descriptor>
  outputs:
    - <output descriptor>
  warnings: []   # 致命的ではないが注意すべき事項
  errors: []     # status=failed 時に必須
```

## 3. 既知の contract 一覧

| Contract | 返却元 | 内容 |
|---|---|---|
| `ISSUE_AUTHOR_COVERAGE_V1` | `issue-author` SubAgent / `create-issue` skill | 起票 Issue のカバレッジ集計 |
| `POST_MERGE_CLEANUP_REPORT_V1` | `post-merge-cleanup-worker` SubAgent | cleanup 結果 + follow-up 候補 |
| `PR_REVIEW_VERDICT_V1` | `pr-reviewer` SubAgent | APPROVE / REQUEST_CHANGES + 根拠 |
| `TEST_RUN_REPORT_V1` | `test-runner` SubAgent | AC ごとの PASS / FAIL |
| `ADVERSARIAL_REVIEW_REPORT_V1` | `adversarial-review` skill | CRITICAL / HIGH / MEDIUM / LOW 件数と詳細 |

各 contract の詳細フィールドは、それぞれの skill / SubAgent の SKILL.md 内で定義する。

## 4. パース時の注意

- メインセッション（または orchestrator）は YAML を読んだら **構造化情報だけ** を残し、SubAgent の散文出力は捨てる
- 失敗時は `status` と `errors` を見て次アクションを決める
- バージョン文字列（`_V1`）の進化は破壊的変更時のみ（後方互換維持を優先）

## 5. fail-closed の原則

- 必須フィールドが欠ける返却は、orchestrator 側で「不正ペイロード」として扱い、SubAgent を再呼び出しせず人間判断を仰ぐ
- 「とりあえず動かす」目的で `status: ok` を勝手に補完しない

## 関連

- [`handoff-contract.md`](handoff-contract.md)
- `.agents/rules/subagent-design-policy.md`
