# ALLOWED_PATHS_GATE_RESULT_V1（pr-review-judge 消費）

snapshot freshness 用の `base_sha_at_snapshot` と、changed files 算出用の `diff_base_sha` を分離して扱う。
local fallback は `changed_files_source: git_diff_current_merge_base_head` とし、
`git diff --name-only <diff_base_sha>...<head>` で changed files を取得して contract の `Allowed Paths` と照合する。
`diff_base_sha` は `git merge-base <current_base_tip> <head_sha>` 相当の SHA を指し、snapshot base を diff 算出には使わない。

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

## provenance

- `contract_fingerprint.base_sha_at_snapshot`: snapshot freshness 判定専用
- `diff_base_sha`: changed files 算出専用
- `base_sha`: `diff_base_sha` の backward-compatible alias
- `changed_files_source`: `git_diff_current_merge_base_head` など source authority が分かる値

## matcher v2 grammar（マッチャ v2 文法）

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

この grammar は GitHub Actions や gitignore の path pattern と完全互換ではなく、
Allowed Paths gate を fail-closed に保つための segment-only な safe subset として扱う。
そのため `docs/**/*.md`、`**.js`、`**/*-post.md` のような partial-segment glob は、
外部仕様では一般的でも本 matcher では invalid / fail-closed である。
