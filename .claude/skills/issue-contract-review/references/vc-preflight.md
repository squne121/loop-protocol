# VC Preflight (baseline_vc_preflight.py)

## 前提

`## Verification Commands` は fenced bash block 形式。

- コマンド行: `AC` マーカー直前に `# ACn`
- 1 行あたり 1 コマンド
- VC でない書式（インラインなど）は無視

## 実行

```bash
uv run python3 .claude/skills/issue-contract-review/scripts/baseline_vc_preflight.py \
  --issue <番号> --repo <owner>/<repo>
```

## 判定

- `status: pass` → OK
- `status: blocked` → BLOCKED
- `status: human_judgment` → `human_escalation`

## Scope Classes

- `baseline_fail_expected`: 基本想定。`expected_fail` を go、想定外 pass は `blocked`
- `regression_gate`: `pnpm ...` / `uv run pytest` 等。pass は go、fail は blocked
- `pr_review_only`: skipped/go（`verification_owner` + `deferred_reason`）
- `runtime_only`: skipped/go（`verification_owner` + `deferred_reason`）

## 主要カテゴリ

`expected_baseline_fail`, `unexpected_pass`, `env_missing_dep`, `command_not_allowed`, `unsupported_syntax`, `compound_command_disallowed`, `file_not_found_*`, `trivially_pass`, `regression_gate` など。

`scope_class` / `classification` / `decision` / `category` を別々に解釈。

## preflight-scope marker

```bash
# AC1
# preflight-scope: pr_review_only
$ <command>
```

- `pr_review_only` / `runtime_only` のみ有効
- 不正値は `classification: human_judgment`。

## github_metadata_assert（GitHub milestone metadata の exit code assertion）

GitHub milestone metadata（特に `description`）の forbidden phrase の有無を **exit code** で検証したい場合は、raw `gh api` を VC に書かず、first-class な `github_metadata_assert` を使う。

許可される形:

```bash
# AC1
$ github_metadata_assert not_contains description <literal> repos/<owner>/<repo>/milestones/<number>
```

- assertion: `contains` / `not_contains` のみ
- 内部実行は固定 argv `gh api --method GET repos/<owner>/<repo>/milestones/<number>`（method GET 固定・非 mutating）
- endpoint は `repos/<owner>/<repo>/milestones/<number>` のみ（絶対 URL・query string `?`・path traversal `../`・placeholder `<...>` は reject）
- `contains` は present→exit 0 / absent→non-zero、`not_contains` は absent→exit 0 / present→non-zero
- gh 不在 / auth 失敗 / 404 / rate limit / timeout / invalid JSON は `github_metadata_assert_environment_error` として `human_judgment` に分類され、assertion の pass/fail（false pass）と区別される

禁止例（VC に raw `gh api` を書かない）:

```bash
# 不可: raw gh api は allowlist で block される。jq は出力するだけで assertion にならない
$ gh api repos/owner/repo/milestones/1 --jq '.description'
```
