このフィクスチャは AC7 の再現用データであり、pytest がテスト対象を1件も収集できない場合（exit code 5）の挙動を検証するために使う。以下のコマンド自体は変更しないこと。

## Verification Commands

```bash
# AC1
$ pytest --collect-only -q docs
```
