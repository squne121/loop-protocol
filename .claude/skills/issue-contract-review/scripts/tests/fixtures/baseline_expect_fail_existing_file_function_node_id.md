Issue #2515 の回帰テスト用 fixture。既存ファイルへの未作成 top-level 関数 node-id を参照する pytest VC に、明示的な `# baseline-expect: fail` 宣言を付与したケースを表す。

## Verification Commands

```bash
# AC1
# baseline-expect: fail
$ uv run pytest .claude/skills/issue-contract-review/scripts/tests/test_baseline_vc_preflight.py::test_function_does_not_exist_2515_issue_marker -q
```
