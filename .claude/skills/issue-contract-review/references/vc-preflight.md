# VC Preflight（baseline_vc_preflight.py の説明）

## 前提

`## Verification Commands` は fenced bash block 形式で記述する規約とする。

- コマンド行: `AC` マーカー直前に `# ACn`
- 1 行あたり 1 コマンド
- VC でない書式（インラインなど）は無視

## 実行

実行コマンド例（Issue 番号と repo を指定して実行する）:

```bash
# 実行例
uv run python3 .claude/skills/issue-contract-review/scripts/baseline_vc_preflight.py \
  --issue <番号> --repo <owner>/<repo>
```

## 判定

判定結果の対応関係は次の通りとする。

- `status: pass` → OK（合格）
- `status: blocked` → BLOCKED（阻止）
- `status: human_judgment` → `human_escalation`（人間判断への委譲）

## Scope Classes（分類の種類）

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

## preflight-scope marker（プリフライト範囲マーカー）

マーカーの記述例:

```bash
# AC1
# preflight-scope: pr_review_only
$ <command>
```

- `pr_review_only` / `runtime_only` のみ有効
- 不正値は `classification: human_judgment`。

## github_metadata_assert（GitHub milestone metadata の exit code による検証機能）

GitHub milestone metadata（特に `description`）の forbidden phrase の有無を **exit code** で検証したい場合は、raw `gh api` を VC に書かず、first-class な `github_metadata_assert` を使う。

許可される形:

許可される記述例:

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

## Command-level timeout budget（Issue #2233、コマンド単位の実行時間上限、fix_delta 反映済み）

canonical VC plan（`compute_canonical_vc_plan()`、schema `canonical_vc_plan/v2`）は、`body_sha256` / `parser_contract_version` / `command_occurrence_count` / `command_occurrences`（occurrence 順の command_hash/is_pure 一覧）などと並ぶ一級のデータとして `command_budgets[]`（`command_timeout_budget/v1` スキーマ）・`aggregate_timeout_seconds`・`plan_digest`（`estimator_evidence_digest` はその alias）を持つ。

この plan は単一の root-owned producer artifact であり、`baseline_vc_preflight.py` 自身の executor（`_main_impl()`）・`contract_readiness_check.py`・`run_contract_review_once.py`・`run_root_review_pipeline.py` の 4 箇所すべてが、同じ pinned Issue body から同一関数 `compute_canonical_vc_plan()` を呼び出す（または、subprocess 境界を跨ぐ場合は同じ body から再計算し `plan_digest` を照合する）ことで、独立に per-command scalar を再導出しない契約になっている（AC2）。

`command_timeout_budget/v1` の各フィールド:

- `command_hash` / `command_identity_hash`: 対象コマンド**テキスト**の識別子（`compute_command_hash()` と同一値）。fix_delta で `execution_key_hash` から改名した。既存の `compute_execution_key_hash()`（argv/cwd/env/timeout/state epoch を含む、実行単位の識別子）とは意図的に別物であり、混同しない。
- `timeout_seconds` / `cleanup_tail_seconds`: 実効 timeout と post-SIGTERM cleanup tail（`CLEANUP_TAIL_SECONDS`、既定 15 秒）
- `source`: `explicit_override`（`--timeout-seconds` の hard global override）/ `static_policy`（`STATIC_PER_COMMAND_TIMEOUT_POLICY` に一致する trusted かつ hand-curated な既知の低速コマンド。150 秒を超える正当な budget を production で生成できる唯一の経路）/ `static_fallback`（既定値、`DEFAULT_PER_COMMAND_TIMEOUT_SECONDS`）。`history_estimate` は fix_delta で production schema から除外した（根拠のない `sample_count=1`/`observed_p95_ms` の捏造を防ぐため）。history-based estimator の再導入は別 Issue で real evidence object と共に行う。
- `estimator_version` / `estimator_input_digest`: provenance の再現性を保証する識別子
- `sample_count` / `observed_p95_ms`: history-based estimator 用の予約フィールド（本 Issue のいずれの production source でも常に `0` / `null`）
- `policy_clamped`: `static_fallback` / `static_policy` の値が `MIN_PER_COMMAND_TIMEOUT_SECONDS` の floor まで引き上げられた場合に `true`（fix_delta で実装。以前は常に `false` だった）

### Override precedence（AC4、優先順位）

`--timeout-seconds` を明示的に指定した場合、それは **hard global override** としてすべてのコマンドの budget に優先適用される（`source: explicit_override`）。明示指定がない場合、`STATIC_PER_COMMAND_TIMEOUT_POLICY` に一致するコマンドは `static_policy`、それ以外は `DEFAULT_PER_COMMAND_TIMEOUT_SECONDS`（`static_fallback`）が使われる。

### 271.31 秒問題の解消（fix_delta P0-2）と Policy ceiling（AC5）

Issue #2233 の元々の障害（`uv run --locked pytest .claude/skills/issue-refinement-loop/tests -v` が実測 271.31 秒であるにもかかわらず固定 150 秒キャップで誤って kill される）は、`STATIC_PER_COMMAND_TIMEOUT_POLICY` に当該コマンドの exact command text を trusted static policy entry として 420 秒（実測値の 1.5 倍以上のマージン）で登録することで解消した。history-based estimator（本 Issue の Out of Scope）ではなく、人間がレビューし version control された固定表であり、実測値の根拠をコメントに記載する。

`MAX_PER_COMMAND_TIMEOUT_SECONDS` は fix_delta で 150 秒から 600 秒に引き上げた（`STATIC_PER_COMMAND_TIMEOUT_POLICY` の全エントリを常に上回るよう import 時に assert される）。この値を超える解決値は、`source`（override / static_policy / fallback のいずれか）を問わず `CommandTimeoutExceedsPolicyError`（`command_timeout_exceeds_policy`、non-retryable）として subprocess 起動前に reject される。`resolved_seconds <= 0` は `source` を問わず `CommandTimeoutNonPositiveError`（`command_timeout_non_positive`）として同様に reject される（fix_delta で追加）。`baseline_vc_preflight.py` の CLI では、これらの違反を `status: blocked` / `failure_class: <error_code>` の JSON として出力し、対象 VC の subprocess は一切起動しない。

`aggregate_timeout_seconds`（plan 全体の per-command budget 合計）が `MAX_TOTAL_VERIFICATION_BUDGET_SECONDS` を超える場合も、`compute_canonical_vc_plan()` 自体が `AggregateTimeoutExceedsPolicyError`（`aggregate_timeout_exceeds_policy`）を **plan を返す前に** raise する（fix_delta P1-1: 以前は informational constant に留まり production では未使用だった）。これは #2207/PR #2221 の aggregate timeout budget 計算式（`effective_n * (150 + 15) + margins`、`contract_readiness_check.derive_review_budget()`）とは別の、command-level budget 自身に対する独立したハード上限であり、#2207 の計算式自体は本 Issue のスコープ外のまま変更していない。

### Consumers と outer wrapper timeout（AC2、消費者側と外側の待機時間、fix_delta P0-1/P0-2）

`contract_readiness_check.py` / `run_contract_review_once.py` / `run_root_review_pipeline.py` は、`DEFAULT_TIMEOUT_SECONDS`（per-command fallback）と `CLEANUP_TAIL_SECONDS`（cleanup tail）を `baseline_vc_preflight.py` からの import のみで参照し、独立した scalar を再定義しない。3 箇所すべてが `baseline_vc_preflight.compute_canonical_vc_plan()` を直接呼び出し（re-export のみ）、同一の canonical plan を消費する。

- `contract_readiness_check.run_baseline_vc_preflight()` は、`baseline_vc_preflight.py` を subprocess 起動する際に `--expected-plan-digest` を明示的に渡し、子プロセスが自分自身の `--body-file` から再計算した plan の digest と照合する（不一致は `vc_plan_digest_mismatch` として subprocess 起動前に reject）。
- `run_contract_review_once.py` は Step 5 で `--timeout-seconds` を**無条件には**渡さなくなった（fix_delta P0-1: 以前は常に `explicit_override` を強制し、`static_policy` budget を握りつぶしていた）。同様に `--expected-plan-digest` を渡す。
- `contract_readiness_check.effective_review_budget()`（fix_delta P0-2 で追加）は、`derive_review_budget()`（#2207 の計算式そのものは不変）の結果を、同じ plan の `aggregate_timeout_seconds` を下限として底上げする。この下限は `static_policy` 由来の budget が無い通常のケースでは常に no-op（マージン定数を #2207 formula 側と一致させているため）であり、`static_policy` が実際に per-command 150 秒超を要求する場合にのみ outer wrapper（`run_baseline_vc_preflight()` の supervisor timeout、`run_root_review_pipeline.py` が `reviewer_transport` へ渡す `per_attempt_deadline`/`total_deadline` を含む）を底上げする。

### Result item provenance（AC6、実行結果の来歴情報）

`baseline_vc_preflight.py` の各 VC result item は `timeout_provenance`（`timeout_seconds` / `cleanup_tail_seconds` / `source` / `estimator_version` / `estimator_input_digest`）を持ち、その VC 自身の `command_timeout_budget/v1` エントリから導出される（他コマンドの budget のコピーではない）。

### Out of Scope（対象外の範囲）

history-based（実測 P50/P95 等）の calibration（command identity・環境 fingerprint・minimum samples・TTL・poisoning 防止を含む）は本 Issue の対象外。fix_delta で production schema から `history_estimate` source を除外し、根拠のない provenance の捏造を防いだ（将来の再導入は real evidence object と共に別 Issue で行う）。
