# ALLOWED_PATHS_GATE_RESULT_V1（pr-review-judge 消費）

snapshot freshness 用の `base_sha_at_snapshot` と、changed files 算出用の `diff_base_sha` を分離して扱う。
canonical な判定 input は `audited_paths[]`（`changed_file_records[]` から派生）であり、
`changed_files[]`（post-image filename のみ）は backward-compatible alias に過ぎない。

```yaml
changed_files_source_policy:
  preferred_oracle:
    - github_pull_request_files_api_with_previous_filename
  deterministic_local_fallback:
    - git_diff_name_status_find_renames_z
  insufficient_for_rename_provenance:
    - gh_pr_diff_name_only
    - git_diff_current_merge_base_head_name_only
  forbidden:
    - git_diff_snapshot_base_head
    - post_image_filename_only_for_rename_gate
```

local fallback は `git diff --name-status -M -z <diff_base_sha>...<head_sha>` を parse し、
`R*`（rename）/ `C*`（copy）status の old/new path を両方 `audited_paths` に含める。
`-z` は tab / newline を含む path を安全に扱うための必須オプションであり、
line-split ベースの parser は使わない。

`diff_base_sha` は `git merge-base <current_base_tip> <head_sha>` 相当の SHA を指し、snapshot base を diff 算出には使わない。
local fallback は `current_base_sha` と `head_sha` から evaluator 内で `git merge-base` を計算・検証できた場合だけ
`git_diff_name_status_find_renames_z` を名乗る。外部入力の `diff_base_sha` が計算値と一致しない場合は
`indeterminate` とし、snapshot base を diff 算出へ流用してはならない。

GitHub PR files API（`--pr-files-json`）を preferred oracle として使う場合は、
呼び出し側が pagination を完走し、`status: renamed` の record に `previous_filename` を必ず含める。
`pagination_complete: false` / `file_limit_reached: true` / renamed record に `previous_filename` が
欠落している場合は `indeterminate` とする（filename-only fallback へ倒してはならない）。

Status 定義:

- `ok`: audited_paths すべて許容
- `fail_closed`: 逸脱あり（必須 blocker）
- `stale_snapshot`: snapshot と現状が不一致
- `indeterminate`: preflight 不足/ head mismatch/ snapshot 不完全/ rename provenance 不足

## matcher（要点）

- `src/**` は再帰一致
- `docs/*` は1セグメント一致
- invalid path（`..`,`absolute`,`backslash`）は fail-closed

## 結果反映

`indeterminate/fail_closed` は merge-blocking として扱い、`REQUEST_CHANGES` 経路。

## provenance（由来情報）

- `contract_fingerprint.base_sha_at_snapshot`: snapshot freshness 判定専用
- `diff_base_sha`: changed files 算出専用
- `base_sha`: `diff_base_sha` の backward-compatible alias
- `changed_files_source`: `git_diff_name_status_find_renames_z` /
  `github_pull_request_files_api_with_previous_filename` など source authority が分かる値
- `changed_file_records[]`: `path` / `status` / `previous_path` / `source` / `provenance_complete` を持つ
  構造化 record（`ChangedFileRecord`）。canonical な audit trail
- `audited_paths[]`: `path` / `path_role`（`filename` | `previous_filename`）/ `source_record_index` を持つ、
  Allowed Paths 判定の canonical input
- `violations[]`: `file` / `path_role` / `reason` を持つ

## matcher v2 grammar（マッチャ v2 文法）

matcher v2 grammar は、外部 dependency なしの segment-based マッチャである。
パターンとパスを `/` で segment に分割し、各 segment を以下の規則で照合する。

- literal segment: ちょうど 1 つの segment に完全一致するリテラルである
- `*`: ちょうど 1 つの segment に一致する
- `**`: segment 全体としてのみ許可し、0 個以上の segment に一致する（動的計画法で再帰的に照合する）

partial-segment glob（`*.md`, `foo*`, `**suffix`, `***`）は invalid であり、fail-closed として扱う。
invalid path（absolute, backslash, `..`, empty segment）も fail-closed である。
`docs/*` は 1 segment 一致、`src/**` は再帰一致という既存挙動を維持しつつ、
`.claude/skills/**/SKILL.md` や `docs/**/README.md` のような mid-path `**` を決定論的に判定できる。
Python 3.12 互換であり、`pathlib.PurePath.full_match` / `glob.translate` / `fnmatch` には依存しない。

この grammar は GitHub Actions や gitignore の path pattern と完全互換ではなく、
Allowed Paths gate を fail-closed に保つための segment-only な safe subset として扱う。
そのため `docs/**/*.md`、`**.js`、`**/*-post.md` のような partial-segment glob は、
外部仕様では一般的でも本 matcher では invalid / fail-closed である。

## リネーム元 provenance（`previous_filename`）監査（Issue #1300）

rename を示す `ChangedFileRecord`（`status: renamed`）は、post-image path（`filename` ロール）と
pre-image path（`previous_filename` ロール）の両方を `audited_paths` に追加し、双方を Allowed Paths 判定に含める。

- rename 元（`previous_filename`）が Allowed Paths 外なら、rename 先が内でも `fail_closed`
- rename 先（`filename`）が Allowed Paths 外なら、rename 元が内でも `fail_closed`
- rename 元・先が両方 Allowed Paths 内なら `ok`
- `status: renamed` なのに `previous_path` を取得できない record は `indeterminate`
  （filename-only fallback で `ok` に倒してはならない）
- invalid な `previous_path`（`..`, absolute, backslash 等）も `indeterminate`

`update_branch` 相当の DAG（base 側に unrelated file、feature 側で rename）でも、
base 側の unrelated file は `changed_file_records` / `audited_paths` に混入しない
（`diff_base_sha` は常に `git merge-base(current_base_sha, head_sha)` を指すため）。
