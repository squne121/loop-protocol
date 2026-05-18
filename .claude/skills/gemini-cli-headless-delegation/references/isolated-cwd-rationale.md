# Isolated Temp CWD の設計根拠

## なぜ `tempfile.TemporaryDirectory` を使うのか

`run_gemini_headless.py` の `_run_gemini()` 関数（`scripts/run_gemini_headless.py:268`）は、
Gemini CLI を空の一時ディレクトリ（`cwd=temp_dir`）から起動する。

### 理由

repo root には `GEMINI.md` が存在する。Gemini CLI はプロセス起動時に `GEMINI.md` や `.gemini/`
を **ambient workspace context** として自動読み込みする仕様を持つ。

wrapper が repo root または任意のサブディレクトリを `cwd` として渡した場合、
Gemini CLI がそのディレクトリの `GEMINI.md` / `.gemini/` 設定を読み込み、
request JSON で明示した `context_files` 以外の情報が混入する。

isolated temp cwd（空ディレクトリ）で起動することで、
`GEMINI.md` や `.gemini/` の影響を完全に遮断し、
`context_files` のみを唯一の入力として保証する。

## Temp Dir Cleanup の動作

`tempfile.TemporaryDirectory` を `with` 文（context manager）で使うため、
`with` ブロックの終了時（正常終了・例外発生どちらの場合も）に一時ディレクトリが自動削除される。

```python
# run_gemini_headless.py:268
with tempfile.TemporaryDirectory(prefix="gemini-headless-") as temp_dir:
    return subprocess.run(command, cwd=temp_dir, ...)
```

`subprocess.run()` が完了した時点で `with` ブロックを抜け、`temp_dir` が即座に削除される。

## `.gemini/` 配下に追加設定ファイルが増えた場合の対処方針

isolated temp cwd は常に空ディレクトリとして生成されるため、
`.gemini/` に新規設定ファイルが追加されても wrapper 側の対処は不要。

ただし、Gemini CLI が `$HOME/.gemini/` を参照する場合は isolated cwd では防げない。
preflight スクリプトで `$HOME/.gemini/` の存在を検出し warning を出す対応を Follow-up Issue で予定している。
現時点では、`$HOME/.gemini/` が存在する場合はその設定内容を手動で確認し、
ambient context として混入する情報がないかをレビューすること。
