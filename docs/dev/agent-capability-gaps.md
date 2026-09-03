# Agent Capability Gaps

このドキュメントは、Claude-GPT lane（`scripts/claude-gpt/launch.sh` 経由の
ChatGPT subscription auth backend）と Native Claude lane の間で観測された
capability parity の検証結果を記録する。研究専用（research-only）の記録であり、
guard / launcher の実装修正はここでは行わない。実装修正が必要な場合は
follow-up implementation Issue を作成しリンクする。

## Issue #2436 の検証記録: Claude-GPT / Native Claude parity（agent-retrospective bash guard）

### 結果サマリ

```yaml
result_status: parity_failed
parity_verified: false
tested_head: aade97858e348985081156afab134c317faac67f
availability_probe: available
deterministic_guard_matrix_passed: true
claude_gpt_live_deny_passed: false
claude_gpt_live_allow_passed: false
nested_claude_proxy_transport_proven: false
```

> **本セクションは Issue #2436 の元の測定結果のみを記述する（`tested_head`
> 固定、変更なし）**: 上記4フィールドはいずれも Issue #2436 で `verified` と
> 判定するために必要な必要条件（全件成立が条件）であり、
> `claude_gpt_live_deny_passed` / `claude_gpt_live_allow_passed` /
> `nested_claude_proxy_transport_proven` が `false` のままである限り、
> `result_status` は `parity_failed` のまま据え置く（OWNER 敵対的レビュー
> https://github.com/squne121/loop-protocol/pull/2453#issuecomment-5469097778
> P0-1 指摘に基づく訂正）。Issue #2445 が修正・観測した内容は、この4フィールド
> のうちどれか単体を `true` に書き換えることでは表現せず、下記
> 「Issue #2445 post-fix re-verification」セクションに、本セクションとは
> 独立した、狭いスコープの別レコード（`transport_routing_gap_status`）として
> 追記する。

- 検証日時（`timestamp`）: 2026-08-30（日本時間、セッション実施日）
- 対象リポジトリ（`tested_repository`）: squne121/loop-protocol
- 検証対象コミット（`tested_head`）: `aade97858e348985081156afab134c317faac67f`
  （`origin/main`、PR #2425 merge 後の直近 HEAD。`.claude/skills/agent-retrospective/**` /
  `scripts/claude-gpt/**` に PR #2425 以降の semantic change なし）
- 関連 Issue（`related_issues`）: #2436（本件）, #2419（guard incident の起源）,
  #2425（P0 bypass fix, merged）, #2204 / PR #2205（SKIP/transport evidence
  semantics の先例）, **#2445（本調査で作成した follow-up implementation issue）**
- 秘匿情報の取り扱い方針（`redaction_policy`）: token / OAuth credential /
  raw prompt / raw model response は記録しない。proxy 構造化ログの `reqId` /
  `path` / `status` / `transport` フィールドのみ参照する。

### AC1: 決定論的ガード検証（deterministic guard matrix）— PASS

```yaml
deterministic_guard_matrix_passed: true
```

```
uv run --locked pytest \
  .claude/skills/agent-retrospective/scripts/tests/test_bash_mutation_denial_canary.py \
  .claude/skills/agent-retrospective/scripts/tests/test_bash_readonly_pipeline_allowed_canary.py \
  -v
```

結果: **72 passed, 0 failed**（`tested_head` 上で fresh 実行）。

再現規約カバレッジ（PR #2425 の P0 bypass class を含む）を確認済み:

- プロセス置換（process substitution）の迂回拒否: `cat <(git merge stale-feature)`
- `find -exec` / `-execdir` 経由の迂回拒否: `find . -maxdepth 1 -exec git merge stale-feature ;`
- Python/uv 間接実行の迂回拒否: `python3 -c "os.system(...)"`, `uv run python3 script_that_merges.py`
- 改行区切り（newline separator）の迂回拒否: `ls\ngit merge stale-feature`
- git inline alias/config を使った迂回拒否: `git -c alias.merge-x=merge merge-x stale-feature`,
  `git --config alias.mx=merge mx stale-feature`
- git 未列挙 mutation（denylist 漏れ対策）の拒否: `git add`, `git hash-object -w`,
  `git bisect start`
- `gh` action-position spoof（位置引数偽装）の拒否: `gh pr checkout 1 --branch view`,
  `gh workflow run triage.yml --ref view`
- canonical AGY capability の許可確認: `test_bash_guard_hook_allows_canonical_agy_builder_invocation*`
- read-only な git pipeline / GitHub コマンド / `gh api` GET の許可確認
- hook 内の想定外例外に対する fail-closed 挙動の確認: `test_bash_guard_hook_exits_2_on_unexpected_exception`

この結果自体は、この検証で発見された下記の transport routing gap の影響を
受けない（`retrospective_bash_guard_hook.py` を直接 subprocess として起動する
決定論的テストであり、backend/proxy の識別とは独立している）。

### AC2: Claude-GPT 可用性確認（availability probe）— available（利用可能）

```yaml
availability_probe: available
```

```
scripts/claude-gpt/launch.sh --check-only
```

- 終了コード（exit code）: `0`
- ChatGPT 認証状態（`chatgpt_auth.available`）: `true`（`detail: "authenticated"`）
- proxy の絶対パス（`proxy.absolute_path`）: `/home/squne/.local/bin/claude-code-proxy`
- proxy のバージョン（`proxy.version`）: `claude-code-proxy 0.1.34`
- パス検証結果（`canonical_paths.ok` / `read_restriction.ok`）: いずれも `true`
- launcher の識別情報: `scripts/claude-gpt/launch.sh`
  の sha256 は `e006608d9999741adfcf7b3be230f0be8043128689c042413132fb22c905fa44`
- Claude CLI の識別情報: `2.1.251 (Claude Code)` (`/home/squne/.local/bin/claude`)

secret/token の値は記録していない。

### AC3/AC4/AC5: parity_failed — nested claude invocation に Claude-GPT proxy routing 手段が存在しない

```yaml
claude_gpt_live_deny_passed: false
claude_gpt_live_allow_passed: false
nested_claude_proxy_transport_proven: false
```

#### 実施内容と、当初の誤判定

`scripts/claude-gpt/lib.sh` の `claude_gpt_build_proxy_env` と同一の env
allowlist（`PATH`/`HOME`/`CCP_CONFIG_DIR`/`XDG_STATE_HOME`/`CCP_BIND_ADDRESS`/
`CCP_LOG_STDERR`/`CCP_CODEX_TRANSPORT`）で実 `claude-code-proxy 0.1.34` を
起動し（`chatgpt_auth: authenticated` 済み）、呼び出し元シェルに
`ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_MODEL` 等を
export した状態で、production `run_retrospective.py` の `invoke_agent()` /
`build_agent_invocation_argv()` 経由（run-scoped `--settings` で
`retrospective_bash_guard_hook.py` を PreToolUse hook として注入する経路）で
既存の live canary test（`test_run_retrospective_live_cli.py` の
`test_real_claude_cli_bash_guard_denies_git_merge_and_repo_unchanged` /
`test_real_claude_cli_bash_guard_allows_readonly_pipeline`）を呼び出した。

当初、live allow は1回目の実行で PASS（独立計算した sha256 と model 報告値が
一致）、live deny は計2回の実行（6 trial）が self-refusal で skip した後、
3回目の実行で PASS（`permission_denials` に Bash `merge` denial 記録、
disposable repo の ground truth 不変も確認）した。またこれらの実行前後の
byte-offset window で `scripts/claude-gpt/transport_log.py` を実行したところ
`ok: true`（`http_count: 31`）が得られたため、一時的に
`result_status: verified` として記録した。

#### 独立レビュー（SubAgent C）による是正

その後の敵対的レビューで、上記の「Claude-GPT lane を経由した」という前提
そのものが誤りであることが判明した。根拠（`tested_head` 上でソースコードから
静的に確定可能。実行時トレース不要）:

```
grep -n "_ENV_PASSTHROUGH_ALLOWLIST\|_RUN_SCOPED_ENV_PREFIX\|sanitize_subprocess_env" \
  .claude/skills/agent-retrospective/scripts/run_retrospective.py
# 2951:_ENV_PASSTHROUGH_ALLOWLIST = frozenset({"PATH", "HOME", "LANG", "LC_ALL", "TZ"})
# 2954:_RUN_SCOPED_ENV_PREFIX = "AGENT_RETROSPECTIVE_"
# 3354:    def sanitize_subprocess_env(self, env: dict[str, str]) -> dict[str, str]:
```

`DelegatedAgentPermissionPolicy.sanitize_subprocess_env()`
(`run_retrospective.py:3354-3368`) は、nested `claude` subprocess へ実際に
渡す env を `_ENV_PASSTHROUGH_ALLOWLIST`（`PATH`/`HOME`/`LANG`/`LC_ALL`/`TZ`）
と `_RUN_SCOPED_ENV_PREFIX`（`AGENT_RETROSPECTIVE_`）のみに限定する。
`ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_MODEL` /
`ANTHROPIC_DEFAULT_*_MODEL` / `CLAUDE_CONFIG_DIR` はいずれにも該当せず、
呼び出し元プロセスの `os.environ` に何を設定していても nested `claude`
subprocess には伝播しない。`HOME` は通過するが、`scripts/claude-gpt/launch.sh`
が実際に claude-gpt 用 settings を書き込む先は `HOME` とは独立した
`CLAUDE_CONFIG_DIR`（`claude_gpt_claude_config_dir()`）であるため、`HOME` の
通過だけでは claude-gpt 側の proxy 設定を継承できない。

結論: **production `run_retrospective.py` の nested-invocation 経路
(`invoke_agent()`) には、Claude-GPT proxy へ nested claude を routing する
手段が構造的に存在しない。** 上記の live deny/allow 実行は、実際には
Native Claude lane（呼び出し元セッションの ambient 実 HOME 配下の通常
credential）で行われていた可能性が高く、Claude-GPT lane の証拠としては
成立しない。同様に、観測された proxy log 上の 31 件の `/v1/messages`
リクエストは、同一 Unix user 上で並行動作していた無関係な別の claude-gpt
セッション由来である可能性が高く（`<CLAUDE_GPT_HOME>/state/claude-code-proxy/proxy.log`
は port/pid 等の instance 識別子を持たない、Unix user 単位で共有される
単一ファイルであることを確認済み）、本検証の nested invocation に
帰属する証拠として採用できない。

よって:

- `claude_gpt_live_deny_passed`: `true` → **`false`** に訂正（Native lane で
  実行された疑いが強く、Claude-GPT lane での deny 証明として採用不可）
- `claude_gpt_live_allow_passed`: `true` → **`false`** に訂正（同上）
- `nested_claude_proxy_transport_proven`: `true` → **`false`** に訂正
  （観測された proxy トラフィックを本検証の nested invocation に
  帰属させる証拠がない）

ガード自体（AC1）にリグレッションは一切ない。これは guard/security の
回帰ではなく、**production の nested-invocation adapter に Claude-GPT
routing の実装が存在しない**という capability gap である。

### Issue #2445 修正後の再検証（post-fix re-verification）

Issue #2445 は上記 `transport_routing_gap` の是正を実装した follow-up
implementation Issue。修正内容（Allowed Paths: `run_retrospective.py` /
新設 unit test / 新設 live verification script /
本ファイルの `parity_failed` エントリ更新のみ）:

- **AC1**: `DelegatedAgentPermissionPolicy.sanitize_subprocess_env()` /
  `_default_sanitized_env()`（旧 `_ENV_PASSTHROUGH_ALLOWLIST`
  allowlist-based semantics）を、`plugins/agent-retrospective/skills/run/scripts/run_retrospective.py`
  の同名関数と同じ **denylist-based semantics**（`_MUTATION_CREDENTIAL_ENV_VARS`
  のみ除外し、それ以外は親環境をそのまま継承）へ置き換えた。新しい
  claude-gpt 専用 opt-in フラグは追加していない。
- **AC2**: `.claude/skills/agent-retrospective/scripts/tests/test_sanitize_subprocess_env_regression.py`
  を新設し、`ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_MODEL` /
  `CLAUDE_CONFIG_DIR` / `CLAUDE_CODE_AUTO_COMPACT_WINDOW` /
  `CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK` /
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` 等の non-mutation env が
  `invoke_agent()` の実 runner env まで到達すること、既存の
  `_MUTATION_CREDENTIAL_ENV_VARS` は引き続き除外されることを、
  `sanitize_subprocess_env()` と `_default_sanitized_env()` の両方について
  回帰確認する（18 tests、うち `-k "default_sanitized_env"` で 13 tests）。
- **AC3**: `.claude/skills/agent-retrospective/scripts/tests/verify_claude_gpt_transport_passthrough.sh`
  を新設し、修正後の実装で PR 時の一回限りの live verification を実施した
  （下記「AC3 live 実施結果」参照）。

#### AC3 live 実施結果（`transport_observed`、排他的帰属は主張しない）

`scripts/claude-gpt/launch.sh` 経由で起動した outer live Claude-GPT
セッション（実 ChatGPT subscription 認証）から、`run_retrospective.py` の
production `invoke_agent()` 経由で `retrospective-runtime-observer`
（frontmatter `model: haiku`）を呼び出す nested invocation を発火させた。
Issue #2436 の反省（同一 Unix user 単位で共有される単一 proxy log による
誤帰属）を踏まえ、**この検証専用の、この検証だけが排他的に所有する
`claude-code-proxy` インスタンス**（`mktemp -d` の scratch `CLAUDE_GPT_HOME`。
実プロファイルの ChatGPT credential のみをコピーして使う。transport log
は実行前に存在しない = 空/新規作成であることを事前確認）を使い、既存の
`scripts/claude-gpt/transport_log.py`（無変更、読み取り専用利用）でその
scoped log を判定した。

証明対象は **`transport_observed`** であり、OWNER 敵対的レビュー
（https://github.com/squne121/loop-protocol/pull/2453#issuecomment-5469097778
P0-2）を踏まえた次の理由により、対象 nested invocation への
**排他的帰属（exclusive attribution）は主張しない**: model alias
（Haiku-tier の `gpt-5.6-luna`）は Claude Code 公式ドキュメントが
background functionality にも用いると明記しており、一意な causal ID として
使えない。実際に live trial では 5/5/8 件の request が観測され、Haiku-tier
（"Luna"）と outer（"Terra"）双方の model-alias traffic が同一 scoped log に
混在した。

3 回の live trial すべてで PASS（exit 0）:

```
started_count: 5 (trial 1) / 5 (trial 2, retest) / 8 (trial 1 回目に発生した
  model の bash 再実行込みの trial)
haiku_alias_match_count (gpt-5.6-luna, 期待される haiku エイリアス): >= 1（毎回確認）
observed_model_aliases: ["gpt-5.6-luna", "gpt-5.6-terra"] のみ
  （gpt-5.6-terra は outer セッション自身の main エイリアス。他の未知
  エイリアスは一度も観測されなかった）
transport_log.py の transport_verdict.ok: true（毎回。malformed 行 0、
  websocket/auto 0、全 reqId が http /v1/messages 200 で確認済み）
fallback_suspected: 該当なし（毎回 haiku エイリアス側の request が観測された）
```

この run-scoped transport log 内で、outer session 自身のトラフィックと
nested invocation が期待する Haiku-tier `/v1/messages` トラフィックの両方が
観測され、観測された全リクエストが期待される transport（proxy）経由で
成功したこと（`transport_observed`）を確認した。対象 nested invocation に
どの単一 reqId が帰属するかを排他的に特定すること（exclusive attribution）
は主張しない。

この結果は、Issue #2436 の `verified` 判定に必要な4フィールド
（本ドキュメント冒頭「結果サマリ」参照）のいずれも `true` に書き換えない。
`nested_claude_proxy_transport_proven` は Issue #2436 の元の（より強い、
排他的帰属を要求する）claim であり、本 Issue #2445 は別の狭いスコープの
claim（`transport_routing_gap_status`）だけを検証・記録する:

```yaml
transport_routing_gap_status: verified
post_fix_tested_head: 60e189af50ed7ba6d2548414139a10e8d71799a0
nested_claude_proxy_transport_observed: true
```

- `transport_routing_gap_status: verified` は、本 Issue #2445 が特定・修正
  した `transport_routing_gap` capability gap（nested claude invocation に
  Claude-GPT proxy routing 手段が存在しないこと）が解消し、上記の live
  verification で `transport_observed` を確認したことのみを意味する。
- `nested_claude_proxy_transport_observed: true` は「排他的に帰属できる
  ログ scope で証明した」ではなく「観測した」という弱い claim である。
- `claude_gpt_live_deny_passed` / `claude_gpt_live_allow_passed` /
  `parity_verified` / `nested_claude_proxy_transport_proven`（Issue #2436
  の元の4フィールド）はいずれも本レコードでは変更しない（`false` のまま、
  スコープ外）。

#### AC4: 再実行手順（re-run procedure）

本エントリの `transport_routing_gap_status: verified` 判定を独立に再現する
場合の手順（Issue #2436 の元の `result_status: parity_failed` 判定はこの
手順では変更しない）:

```bash
# AC1
uv run --locked pytest .claude/skills/agent-retrospective/scripts/tests/test_sanitize_subprocess_env_regression.py -q

# AC2
uv run --locked pytest .claude/skills/agent-retrospective/scripts/tests/test_sanitize_subprocess_env_regression.py -q -k "default_sanitized_env"

# AC3 (実 ChatGPT subscription 認証が必要。利用不能な環境では exit 77 で SKIP)
bash .claude/skills/agent-retrospective/scripts/tests/verify_claude_gpt_transport_passthrough.sh

# AC4 (このエントリ自体の所在確認。Issue #2436 の元の parity_failed 判定が
# 変更されていないことも同時に確認できる)
rg -n "parity_failed|transport_routing_gap_status" docs/dev/agent-capability-gaps.md
```

#### 未解決のまま残る事項（Out of Scope, follow-up 検討）

`claude_gpt_live_deny_passed` / `claude_gpt_live_allow_passed`
（guard 自体の deny/allow 挙動が Claude-GPT lane 経由でも成立することの
独立証明）は、`transport_routing_gap` とは別個の未検証 claim であり、
本 Issue #2445 のスコープ外（Issue 本文の Out of Scope: 「新しい security
harness や claude-gpt-mode classifier の追加」「常設の CI required gate や
permanent runtime-verification harness としての AC3 の恒久化」を除外して
いるため、この2つの再検証は行っていない）。再検証する場合は Issue #2436 の
元の live deny/allow canary（`test_run_retrospective_live_cli.py`）を、
今回修正済みの transport 経路上で改めて実行する。

### Follow-up

Issue #2436 の docs-only / research-only contract に従い、本 Issue の PR では
guard/launcher/adapter の実装修正を行わない。実装修正は follow-up
implementation Issue へ切り出した:

- **#2445**: `run_retrospective.py` の nested claude invocation に
  Claude-GPT proxy env passthrough が存在しない
  （`sanitize_subprocess_env()` の allowlist 拡張 or 代替設計を要検討）。
  再現手順・失敗クラス（`transport_routing_gap`）・関連 Issue
  （#2436 / #2419 / #2425）を Issue 本文に記載済み。

## Issue #2488 の検証記録: agent-retrospective の codebase-investigator observer が live production で structured output を返さない root cause 分類

### 結果サマリ

```yaml
failure_class: version_regression
classification_rule_matched: "1 (current claude --version does not match known-good baseline 2.1.251 / 2.1.247)"
tested_head: 0b0d8f8c190d5e7839a4ba95913f137cb35c46c8
claude_version_tested: "2.1.259 (Claude Code)"
known_good_baseline_versions: ["2.1.251 (PR #2387)", "2.1.247 (PR #2358)"]
version_matches_baseline: false
production_native_schema_runs: 3
production_native_schema_results: ["PASS", "FAIL:malformed_output/native_result_status_not_ok", "FAIL:malformed_output/missing_structured_output"]
flat_control_schema_runs: 1
flat_control_schema_result: "FAIL: subtype=success, structured_output key absent, result field is prose (not JSON)"
production_shape_divergence_across_runs: true
```

### メタデータ

- 検証日時: 2026-09-04（日本時間、07:23〜07:28 JST の連続セッション）
- 対象リポジトリ: squne121/loop-protocol
- 検証対象コミット（`tested_head`）: `0b0d8f8c190d5e7839a4ba95913f137cb35c46c8`（`origin/main` HEAD、専用 worktree
  `issue-2488-agent-retrospective-structured-output` 上で全 run を実施）
- Claude Code version（`claude --version`）: `2.1.259 (Claude Code)`
- 関連 Issue: #2488（本件）、#2374（PR #2387、CLOSED/マージ済み、known-good baseline 2.1.251 の由来）、
  #2358（known-good baseline 2.1.247 の由来）
- 秘匿情報の取り扱い方針: `claude --version` / repository HEAD SHA / wrapper subtype / structured_output
  presence・type / JSON candidate count / effective model 識別子（`claude-haiku-4-5-20251001` /
  `canonicalModel: claude-haiku-4-5`）のみ記録。token・cookie・credential・raw prompt 全文は記録しない。

### AC1: 有界 live reproduction matrix の実行（production native schema 最大3回 + flat control 1回）

同一 repository HEAD（`0b0d8f8c190d5e7839a4ba95913f137cb35c46c8`）・同一 Claude Code version（`2.1.259`）・
同一 model route（`codebase-investigator` frontmatter `model: haiku` → 実行時 `claude-haiku-4-5-20251001`、
`canonicalModel: claude-haiku-4-5`、`provider: firstParty`）・同一 provider/transport（subscription 認証、
`service_tier: standard`）を固定して実行した。

```bash
# AC1(a) 実行コマンド（3回まで）
uv run --locked pytest \
  ".claude/skills/agent-retrospective/scripts/tests/test_codebase_investigator_observer_contract.py::test_live_consumer_smoke_agy_timeout_native_fallback_observer_acceptance" \
  -m claude_live -s -x -v
```

collect 確認: `1/22 tests collected (21 deselected)`（Issue #2488 本文・PR #2387 の記録と一致）。

#### AC1(a) Run 1（`runtime-verification-AC9-20260903T222358Z.log`、`.gitignore:77` により repo 非追跡）

```yaml
exit_code: 0
pytest_verdict: PASSED (1 passed in 28.41s)
result_status: ok
result_reason_code: null
structured_output_presence: present
bundle_accepted: true
```

`invoke_agent_with_role_adapter()` は `status: ok` で `EvidenceBundle`（`observer_result/v1`）に正しく変換された
`structured_output` を返した。native `CODEBASE_INVESTIGATION_RESULT_V1` は schema-valid、`evidence_refs` の
`commit_sha`（`0b0d8f8c190d5e7839a4ba95913f137cb35c46c8`）は `authoritative_base_sha` と一致、
`.python-version` の内容（`3.12`）と sha256 も独立再検証（`_verify_repo_evidence_ref_bytes`）を通過した。

#### AC1(a) 2回目実行の結果（`runtime-verification-AC9-20260903T222520Z.log`）

```yaml
exit_code: 0
pytest_verdict: FAILED (1 failed in 28.67s)
result_status: malformed_output
result_reason_code: "native_fallback_adaptation_failed:native_result_status_not_ok"
structured_output_presence: absent_after_role_adapter_rejection
bundle_accepted: false
```

`run_retrospective.py:1439-1441`（`adapt_native_codebase_investigation_result`）の分岐に到達しており、
native `CODEBASE_INVESTIGATION_RESULT_V1` 自体は `jsonschema.validate` を通過した（`native_result_schema_invalid`
ではなかった）が、native JSON の `status` フィールドが `"ok"` ではなかった（`"failed"` または `"inconclusive"`
のいずれか。role adapter は生の native `status` 値そのものを reason_code に含めないため、この 2 値のどちらかまでの
特定はできなかった。この limitation は「取得できなかった情報」として明記する）。これは Issue #2488 が報告した
`missing_structured_output`（構造化出力そのものが欠落）とは異なる failure shape であり、**構造化出力は生成された
が、モデル自身が `status: ok` 以外を自己申告した**ケースであることを示す。

#### AC1(a) Run 3（診断用一時スクリプトによる同一 production composed path 実行。`run_retrospective.py` の
`invoke_agent_with_role_adapter()` を直接呼び出し、role adapter 内部で保持される `raw_stdout_excerpt` を追加取得。
スクリプト自体は repo 非追跡の one-shot 診断であり commit しない）

```yaml
exit_code: 0
result_status: malformed_output
result_reason_code: >-
  missing_structured_output:{"json_candidate_count": 0,
  "native_schema_valid_candidate_count": 0, "observed_top_level_keys": [],
  "observer_schema_valid_candidate_count": 0, "result_fence_count": 0,
  "structured_output_presence": "absent", "structured_output_type": null,
  "wrapper_subtype": "success"}
wrapper_subtype: success
structured_output_presence: absent
json_candidate_count: 0
native_schema_valid_candidate_count: 0
result_fence_count: 0
```

Run 3 は Issue #2488 の Background セクションが報告した実行2回目の shape（`json_candidate_count: 0`,
`structured_output_presence: "absent"`, `wrapper_subtype: "success"`）と**完全に一致**する。

**AC1(a) 3回の結果サマリ**: PASS / FAIL（native status not ok, schema-valid candidate 存在）/
FAIL（structured_output 完全欠落, candidate 0 件）という、3回とも異なる shape が観測された
（production shape divergence）。

#### AC1(b) flat control schema（1回、production-compatible invocation を基礎に構築）

```bash
# 実行コマンド（build_agent_invocation_argv() の argv 構造・stdin プロンプト渡しを踏襲）
claude -p --agent codebase-investigator --output-format json \
  --json-schema '<2-field flat schema: {"status": enum[ok,failed], "note": string}>' \
  --no-session-persistence <<'EOF'
Read the file .python-version (repo-relative to cwd) using the Read tool and
report status: "ok", note: the exact file content verbatim (no other tools needed).
EOF
```

```yaml
exit_code: 0
wrapper_subtype: success
structured_output_key_present: false
result_field_type: prose_markdown_not_json
result_excerpt: "**Status**: ok\n\n**Note** (exact file content verbatim):\n```\n3.12\n```"
model: claude-haiku-4-5-20251001
canonical_model: claude-haiku-4-5
provider: firstParty
service_tier: standard
permission_denials: ["Read tool denied for .python-version (ad-hoc invocation lacked production's --disallowedTools/--settings run-scoped harness wiring; this denial is an artifact of the standalone AC1(b) invocation shape, not evidence about schema complexity itself)"]
```

flat control（2 required fields のみ）でも `subtype: "success"` かつ `structured_output` キー自体が応答 JSON に
一切存在しない（`null` ですらなく、キーごと欠落）という、production run 3 と同種の failure shape が再現した。
ただし本 run は `--disallowedTools`/`--settings` による production の run-scoped permission 配線
（`build_agent_invocation_argv()`）を完全には再現しておらず、`Read` tool が permission_denied となった影響で
モデルが tool 実行なしで応答した可能性がある。この差異により、本 run 単独を「schema complexity とは無関係に
flat schema でも同一 failure が起きる」ことの確定証拠として過大解釈しない（下記「残る不確実性」参照）。

### AC2: 機械的 classification

Issue #2488 本文の「機械的classification rule」を順に評価した。

```yaml
rule_1_version_check:
  condition: "current claude --version (2.1.259) matches known-good baseline (2.1.251 or 2.1.247)?"
  result: false
  verdict: "MATCH -- rule 1 fires. failure_class = version_regression (他の診断値に関わらず確定)"
```

**Rule 1 が最初に成立したため、以降の rule 2-6 は評価しない**（Issue 本文の優先順位定義どおり）。

```yaml
failure_class: version_regression
matched_rule: 1
rule_text: >-
  現在の実行環境の claude --version が known-good baseline（2.1.251 または
  2.1.247）のいずれとも一致しない → version_regression を第一failure class候補とする
  （version以外の診断値がどうであれ、まずこの分岐を確認する）
```

**追加の観測事実（Issue 本文「複数の条件が同時に成立しうる場合でも...該当する追加の観測事実は併記してよい」に基づく併記）**:

- production 3 run の shape が分散した（PASS / native-status-not-ok / structured-output-absent）ことは、
  rule 5（`nondeterministic_runtime_failure`）の条件にも合致する観測事実である。
- flat control（1回）も production run 3 と同種の `structured_output` 完全欠落で失敗したことは、
  rule 4（`cli_model_runtime_lane`）の条件にも部分的に合致する観測事実である（ただし上記の permission_denial
  という交絡要因により確定的ではない）。
- production candidate が schema-valid だったが `status` が `"ok"` でなかった run 2 は、rule 2/3
  （`schema_runtime_interaction`）の「production candidate が parseable」という条件には部分的に合致するが、
  これは native schema validation error ではなく native `status` フィールドの自己申告値の問題であり、
  `jsonschema.ValidationError.absolute_path` は該当しない（validation 自体は通過したため）。

これらの追加観測は、`failure_class: version_regression` を上書きしないが、後続 implementation Issue が
version pin/upgrade だけでは解消しない場合に備えるための repair direction の参考情報として記録する。

### AC3: 修復方向（repair direction）の文書化

Issue #2488 本文の repair direction 優先順位に従い、`failure_class: version_regression` の場合の順序
（(1) known-good baseline への version pin/アップグレード検討を最優先、それでも解消しない場合のみ (2) 一般順序）
を適用する。

```yaml
repair_direction_priority:
  step_1: "known-good baseline (2.1.251 or 2.1.247) への version pin、または現行 2.1.259 系列での
    upstream fix 有無の確認を最優先で評価する"
  step_2_if_step_1_insufficient:
    - "structured-output runtime / schema interaction の特定（production run 2 の
      native_result_status_not_ok と run 3 の missing_structured_output は異なるレイヤーの failure
      であり、両方を個別に追跡する必要がある）"
    - "既存 compat recovery（result からの JSON 抽出）で安全に回収可能か
      （run 3 は json_candidate_count: 0 のため compat recovery 対象外。回収可能な candidate が
      存在する場合のみ、既存 local jsonschema.validate + base_sha/evidence verification を
      緩和せずに適用する）"
    - "schema simplification（本 Issue の flat control run は permission_denial という交絡要因を
      含むため、交絡要因を排除した re-run で schema complexity 単独の影響を再検証することが
      次段階の前提条件になる）"
    - "model control / comparison（現行 codebase-investigator frontmatter は model: haiku
      固定。claude-haiku-4-5-20251001 以外のモデルとの比較は本 Issue のスコープ外）"
    - "prompt refinement（最後の候補。仮説B＝prompt競合は #2387 で既に修正済みであり静的証拠で
      再燃していないことを本 Issue でも確認済み）"
```

**外部 upstream evidence（web-researcher fact-check、2026-09-04 実施、現在の一次情報で再検証）**:

- 公式 Claude Code structured output ドキュメント（https://code.claude.com/docs/en/agent-sdk/structured-outputs）は、
  `subtype: "success"` かつ `structured_output` 欠落を現在も documented failure handling として明記
  （"Treat that case as a failure as well"）。"Tips for avoiding errors" も deeply nested schema /
  多数の required fields が failure-prone である旨を現在も明記。
- `anthropics/claude-agent-sdk-typescript#277`（現在 **Open**）: SDK 0.2.97、Haiku 4.5 / Sonnet 4.6 で
  nested schema 時に `subtype: success` かつ `structured_output` 欠落が再現する報告と一致する外形。
- `anthropics/claude-agent-sdk-python#510`（**Closed**、PR #532 で解消）、`#571`（**Closed**、#502 の
  duplicate）、`#502`（現在 **Open**）はいずれも `StructuredOutput` の value 欠落・wrapping 関連の既知問題。
- **訂正**: 本 Issue の refinement 時点で「ハルシネーション疑い」と判定されていた
  `anthropics/claude-code/issues/87234` は、今回の web-researcher 再検証で**実在（現在 Open）**であることが
  確認された（タイトル: `--json-schema` calls emit literal `$PARAMETER_NAME` placeholder keys on toolless
  calls。131 CLI session の統計分析で toolless 呼び出し時 26.8% の placeholder error 率を報告）。
  ただし本 Issue の run 1-3・flat control run はいずれも `--agent codebase-investigator` 経由（toolless
  ではない）であり、#87234 が報告する toolless 特有の現象と直接一致する証拠は本検証では得られていない。
  この food-for-thought は repair direction の追加調査候補として記録するに留め、`failure_class` の確定には
  用いない（2026-09-04 時点で 2026年9月以降の追加 regression/fix/release note は確認できなかった）。

### 残る不確実性

- production run 2 の native `status` フィールドの実際の値（`"failed"` か `"inconclusive"` か）は、
  role adapter が `reason_code` に生の native `status` 値を含めない実装のため特定できなかった。
  再調査する場合は `raw_stdout_excerpt` を全文キャプチャする診断ラッパーが必要。
- flat control run（AC1(b)）は `--disallowedTools`/`--settings` の production run-scoped harness を
  完全には再現しておらず、観測された `Read` permission_denial が結果に交絡している可能性がある。
  schema complexity 単独の影響を再検証する場合は、production と同一の `--disallowedTools`/`--settings`
  argv を flat control にも適用した re-run が必要。
- 3 回の production run で shape が分散したため（PASS 1 / FAIL 2 種）、単一の decisive な root cause
  ではなく、version regression と runtime nondeterminism の複合である可能性が残る。

### AC4: 再現手順（re-run procedure）

```bash
# AC1(a): production native schema（同一 head・同一 claude version で最大3回まで）
uv run --locked pytest \
  ".claude/skills/agent-retrospective/scripts/tests/test_codebase_investigator_observer_contract.py::test_live_consumer_smoke_agy_timeout_native_fallback_observer_acceptance" \
  -m claude_live -s -x -v

# AC1(b): flat control schema（production の --disallowedTools/--settings 配線を含めた再検証が望ましい）
claude -p --agent codebase-investigator --output-format json \
  --json-schema '{"type":"object","required":["status","note"],"additionalProperties":false,"properties":{"status":{"type":"string","enum":["ok","failed"]},"note":{"type":"string"}}}' \
  --no-session-persistence <<'EOF'
Read the file .python-version and report status: "ok", note: the exact file content verbatim.
EOF

# AC2/AC3 の本エントリ所在確認
rg -n "failure_class|#2488" docs/dev/agent-capability-gaps.md
```

### Follow-up

`failure_class: version_regression` の repair direction step 1（known-good baseline への version pin
検討）は、`claude` binary 自体のバージョン管理（システムパッケージ / インストーラ経由）に関わる判断であり、
本リポジトリの `docs/dev/agent-capability-gaps.md` の Allowed Paths（research findings 追記のみ）を超える
実装は本 Issue では行わない。追加観測（production shape divergence、flat control での同型 absent 失敗）は、
version pin/upgrade だけで解消しない場合の repair direction step 2 の出発点として記録した。
follow-up implementation Issue の要否・スコープは、本 Issue のクローズ後に改めて評価する。
