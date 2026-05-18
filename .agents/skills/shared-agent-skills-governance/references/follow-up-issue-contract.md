# Follow-up Issue Contract

`post-merge-cleanup` / `issue-refinement-loop` / その他 orchestrator skill が follow-up Issue を起票する際の canonical contract。`create-issue` skill はこの contract を実行する entrypoint。

## 1. follow-up Issue の定義

ある Issue / PR の作業中に発見された、現スコープに含めるべきでない別問題を新規 Issue として記録すること。

## 2. 必須フィールド

`create-issue` skill 経由で起票する際、以下を必ず満たす。

| フィールド | 内容 |
|---|---|
| `outcome` | follow-up で達成したい状態（1 文） |
| `source` | 発見元の Issue / PR 番号（必須）。例: `discovered in #42 during impl review` |
| `desired_destination` | 起票先のリポジトリ・ラベル群（通常は同一リポジトリ） |
| `validated_scope_delta` | 「現 Issue ではなぜ対応しないのか」の根拠（scope 衝突防止） |
| `proposal_only` | true / false。AI が **本文だけ作って人間判断を待つ** モードか |

`validated_scope_delta` が空 = 「現 Issue で対応すべき」と判定したことを意味し、follow-up Issue は起票しない。

## 3. 出力契約 `ISSUE_AUTHOR_COVERAGE_V1`

create-issue / issue-author SubAgent は以下を返す：

```yaml
ISSUE_AUTHOR_COVERAGE_V1:
  issue_number: 123
  title: "実装: ..."
  template: implementation
  required_sections_present: [背景, 目的, 受け入れ条件, 非ゴール, テスト観点, 変更許可領域]
  required_sections_missing: []
  blocking_stops: []  # [{reason: "Scope分割採否", choices: ["A", "B"]}]
  url: https://github.com/<owner>/<repo>/issues/123
```

## 4. proposal_only モード

- AI が本文を生成するが、`gh issue create` は実行しない
- 出力は YAML だけ、または `tmp/follow-up-<id>-draft.md` への書き出し
- 人間レビューを待ってから起票する場面で使う

## 5. fail-closed の原則

必須フィールドが揃わない場合、起票せずに blocking stop として返す。人間判断を仰ぐ。

## 関連

- [`handoff-contract.md`](handoff-contract.md)
- `.agents/rules/issue-uncertainty-policy.md`
- `.agents/rules/issueops-common-guard.md`
