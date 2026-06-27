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

`pnpm build` は regression gate のまま扱うが、runner 側で fixed env delta `{CI:"true"}` を付けて `shell=False` 実行する。Issue body 側で `CI=true pnpm build` や `env CI=true pnpm build` を書いて回避しない。

## 主要カテゴリ

`expected_baseline_fail`, `unexpected_pass`, `env_missing_dep`, `command_not_allowed`, `unsupported_syntax`, `compound_command_disallowed`, `file_not_found_*`, `trivially_pass`, `regression_gate`, `package_manager_no_tty_prompt` など。

`package_manager_no_tty_prompt` は `ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY` / `Aborted removal of modules directory due to no TTY` を検出したときの dedicated category。body-author-fixable ではなく tooling/env blocker として扱う。

| category | body_author_fixable | downstream_bucket | expected route |
|---|---:|---|---|
| package_manager_no_tty_prompt | false | env_or_runtime | stop rewrite; tooling/human triage |

`scope_class` / `classification` / `decision` / `category` を別々に解釈。

## UV allowlist（実運用固定形）

`uv` の allowlist は次の形のみ許可する。

- `uv lock --check`
- `uv run --isolated --locked --no-default-groups python scripts/ci/runtime_dependency_smoke.py`
- `uv run --isolated --locked --no-default-groups python3 scripts/ci/runtime_dependency_smoke.py`

拒否対象:

- `uv lock` / `uv lock --upgrade` / `uv sync` / `uv run uv lock --check`
- `uv run --isolated --locked python ...` など option が不足する runtime smoke
- `uv run --isolated --locked --no-default-groups --with ... python scripts/ci/runtime_dependency_smoke.py`
- `uv run ... --group ...`, `--with`, `--all-groups`, `--extra`, `--python`, `--project`, `--directory`, `--env-file`, `--upgrade`, `--env-file`
- `uv run --isolated --locked --no-default-groups python -c ...`
- `uv run --isolated --locked --no-default-groups python -m ...`
- `uv run --isolated --locked --no-default-groups python ../runtime_dependency_smoke.py`
- `uv run --isolated --locked --no-default-groups python /tmp/runtime_dependency_smoke.py`
- `uv run --isolated --locked --no-default-groups python scripts/ci/other.py`

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
- field は `description` のみ（allowlist 外・typo は reject）。コマンドは4引数ちょうどで flags / 余分な positional は reject
- 内部実行は固定 argv `gh api --method GET repos/<owner>/<repo>/milestones/<number>`（method GET 固定・非 mutating）
- endpoint は `repos/<owner>/<repo>/milestones/<number>` のみ（絶対 URL・query string `?`・path traversal `../`・placeholder `<...>` は reject）
- `contains` は present→exit 0 / absent→non-zero、`not_contains` は absent→exit 0 / present→non-zero
- gh 不在 / auth 失敗 / 404 / rate limit / timeout / invalid JSON / 未知の gh 失敗 / response に field 不在（schema drift）は `github_metadata_assert_environment_error` として `human_judgment` に分類され、assertion の pass/fail（false pass）と区別される

禁止例（VC に raw `gh api` を書かない）:

```bash
# 不可: raw gh api は allowlist で block される。jq は出力するだけで assertion にならない
$ gh api repos/owner/repo/milestones/1 --jq '.description'
```
