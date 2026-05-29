## Machine-Readable Contract

```yaml
contract_schema_version: "v1"
issue_kind: implementation
parent_issue: none
goal_ref: "example bad vc marker fixture"
change_kind: chore
```

## Outcome

`example_script.py` が `--dry-run` フラグを受け付け、副作用なしで実行結果を出力する。

## Acceptance Criteria

- [ ] AC1: `example_script.py` が存在すること

## Verification Commands

```bash
# AC1: verify the file exists
test -f example_script.py
```

## Allowed Paths

- `example_script.py`

## Stop Conditions

実装中にこれらの状況が発生したら直ちに作業を停止し、Issue comment に状況を記録して人間の判断を待つ。

- Allowed Paths 外の変更が必要と判明した場合
- In Scope の固定契約の変更が必要になった場合
- 新規 Issue の起票が必要と判断した場合
- 後続 Phase への波及が判明した場合
- nested SubAgent delegation が必要になった場合
- 外部サービス利用・権限昇格が必要になった場合
