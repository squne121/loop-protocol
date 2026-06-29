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

## matcher v2 grammar

matcher v2 grammar は、外部 dependency なしの segment-based マッチャである。
パターンとパスを `/` で segment に分割し、各 segment を以下の規則で照合する。

- literal segment: 完全一致する 1 segment
- `*`: ちょうど 1 segment に一致する
- `**`: segment 全体としてのみ許可し、0 個以上の segment に一致する（動的計画法で再帰的に照合）

partial-segment glob（`*.md`, `foo*`, `**suffix`, `***`）は invalid であり、fail-closed として扱う。
invalid path（absolute, backslash, `..`, empty segment）も fail-closed である。
`docs/*` は 1 segment 一致、`src/**` は再帰一致という既存挙動を維持しつつ、
`.claude/skills/**/SKILL.md` や `docs/**/README.md` のような mid-path `**` を決定論的に判定できる。
Python 3.12 互換であり、`pathlib.PurePath.full_match` / `glob.translate` / `fnmatch` には依存しない。
