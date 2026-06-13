# ALLOWED_PATHS_GATE_RESULT_V1（pr-review-judge 消費）

`git diff --name-only <base>...<head>` を取得し、contract の `Allowed Paths` と照合。

Status 定義:

- `ok`: changed files すべて許容
- `fail_closed`: 逸脱あり（必須 blocker）
- `stale_snapshot`: snapshot と現状が不一致
- `indeterminate`: preflight 不足/ head mismatch/ snapshot 不完全

## matcher（要点）

- `src/**` は再帰一致
- `docs/*` は1セグメント一致
- invalid path（`..`,`absolute`,`backslash`）は fail-closed

## 結果反映

`indeterminate/fail_closed` は merge-blocking として扱い、`REQUEST_CHANGES` 経路。
