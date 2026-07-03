このフィクスチャは AC7 の再現用データであり、pytest がテスト対象を1件も収集できない場合（exit code 5）の挙動を検証するために使う。以下のコマンド自体は変更しないこと。

対象ディレクトリは `docs/` のような将来変化しうる実ディレクトリではなく、pytest discovery 対象を絶対に含まない専用の空ディレクトリ （`.claude/skills/issue-contract-review/tests/fixtures/no_collectible_tests_dir/`）を使う（PR #1305 review Blocker: 脆い前提の解消）。

## Verification Commands

```bash
# AC1
$ pytest --collect-only -q .claude/skills/issue-contract-review/tests/fixtures/no_collectible_tests_dir
```
