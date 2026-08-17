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

## Command-level timeout budget（Issue #2233）

canonical VC plan（`compute_canonical_vc_plan()`）は、`body_sha256` / `parser_contract_version` / `command_occurrence_count` などと並ぶ一級のデータとして `command_budgets[]`（`command_timeout_budget/v1` スキーマ）・`aggregate_timeout_seconds`・`estimator_evidence_digest` を持つ。

`command_timeout_budget/v1` の各フィールド:

- `command_hash` / `execution_key_hash`: 対象コマンドの識別子（`compute_command_hash()` と同一値）
- `timeout_seconds` / `cleanup_tail_seconds`: 実効 timeout と post-SIGTERM cleanup tail（`CLEANUP_TAIL_SECONDS`、既定 15 秒）
- `source`: `explicit_override`（`--timeout-seconds` の hard global override）/ `static_fallback`（既定値、`DEFAULT_PER_COMMAND_TIMEOUT_SECONDS`）/ `history_estimate`（将来の history-based estimator 用の予約値。本 Issue のスコープでは production caller は使用しない）
- `estimator_version` / `estimator_input_digest`: provenance の再現性を保証する識別子
- `sample_count` / `observed_p95_ms`: history-based estimator 用の予約フィールド（本 Issue では常に `0` / `null`）
- `policy_clamped`: 将来、値をクランプして返す estimator 用の予約フィールド（本 Issue の実装は clamp ではなく reject 方式を採用するため常に `false`）

### Override precedence（AC4）

`--timeout-seconds` を明示的に指定した場合、それは **hard global override** としてすべてのコマンドの budget に優先適用される（`source: explicit_override`）。明示指定がない場合は `DEFAULT_PER_COMMAND_TIMEOUT_SECONDS`（`static_fallback`）が使われる。

### Policy ceiling（AC5）

`MAX_PER_COMMAND_TIMEOUT_SECONDS`（`DEFAULT_PER_COMMAND_TIMEOUT_SECONDS` と同値、150 秒）を超える値は、`source`（override / estimate / fallback のいずれか）を問わず `CommandTimeoutExceedsPolicyError`（`command_timeout_exceeds_policy`、non-retryable）として subprocess 起動前に reject される。`baseline_vc_preflight.py` の CLI では、この違反を `status: blocked` / `failure_class: command_timeout_exceeds_policy` の JSON として出力し、対象 VC の subprocess は一切起動しない。

この上限は #2207/PR #2221 の aggregate timeout budget 計算式（`effective_n * (150 + 15) + margins`）が前提とする「1コマンドあたり最大150秒」という worst-case 仮定を、command-level budget が決して超えないことを保証するための設計（AC3 aggregate invariant）であり、この計算式自体は本 Issue のスコープ外（変更しない）。

### Consumers（AC2）

`contract_readiness_check.py` / `run_contract_review_once.py` / `run_root_review_pipeline.py` は、`DEFAULT_TIMEOUT_SECONDS`（per-command fallback）と `CLEANUP_TAIL_SECONDS`（cleanup tail）を `baseline_vc_preflight.py` からの import のみで参照し、独立した scalar を再定義しない。`run_root_review_pipeline.py` / `contract_readiness_check.py` はいずれも `baseline_vc_preflight.compute_canonical_vc_plan()` を直接呼び出し（re-export のみ）、同一の canonical plan を消費する。

### Result item provenance（AC6）

`baseline_vc_preflight.py` の各 VC result item は `timeout_provenance`（`timeout_seconds` / `cleanup_tail_seconds` / `source` / `estimator_version` / `estimator_input_digest`）を持ち、その VC 自身の `command_timeout_budget/v1` エントリから導出される（他コマンドの budget のコピーではない）。

### Out of Scope

history-based（実測 P50/P95 等）の calibration（command identity・環境 fingerprint・minimum samples・TTL・poisoning 防止を含む）は本 Issue の対象外。`estimated_seconds` / `history_estimate` source はそのための forward-compatible hook であり、本 Issue の production path では未使用。
