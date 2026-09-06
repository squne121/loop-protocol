Issue #2515 の回帰テスト用 fixture。既存ファイルへの未作成 class node-id を参照する pytest VC で、`baseline-expect` annotation を付与しない場合の既存挙動を表す（回帰確認用）。

## Verification Commands

```bash
# AC1
$ uv run pytest .claude/skills/issue-contract-review/scripts/tests/test_baseline_vc_preflight.py::TestClassDoesNotExist2515IssueMarker -q
```
