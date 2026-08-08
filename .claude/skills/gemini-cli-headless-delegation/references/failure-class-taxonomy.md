---
taxonomy_schema_version: v2
status: draft
related_issue: "#268"
created_at: "2026-05-23"
updated_at: "2026-05-23"
概要: "本文書は failure_class の分類体系と retry policy を定義する仕様文書である"
---

# failure_class Taxonomy and Retry Policy

`gemini-cli-headless-delegation` の preflight および run wrapper の
result JSON で使用する `failure_class` 分類体系と retry policy の仕様。

## Background

Issue #71 の refinement-loop で判明した問題:
- `auth.ok:false` が transient エラー（quota / 一時的 API エラー）と
  真の認証失効を同一フィールドに丸めているため、retry 可否の判断ができない
- config error でも quota error でも同じ fail-close になっている
- caller 側で retry 可否を判断する根拠が result JSON に存在しない

本文書はこの問題を解消するための taxonomy と retry policy を定義する。

後続実装 Issue で `preflight_gemini_headless.py` および `run_gemini_headless.py`
のコードへの反映が行われる。

---

## failure_class の分類体系

`failure_class` フィールドは `nullable: true` で成功時は `null`。

### Non-retryable failures（再試行不可能な失敗）

これらは retry しても同じ結果になる構成・認証・スキーマ問題。
即時 fail-close して human intervention または config 修正を求める。

`retryable: false` のエントリは `retry_scope: none` を持つ。
外部状態変化（auth 修正 / config 修正）による回復経路は
`recovery_scope` / `recovery_action` フィールドで表現する。

| `failure_class` | 意味 | 発生レイヤー | Raw Signal 例 | `recovery_scope` |
|---|---|---|---|---|
| `request_schema_invalid` | `delegation_request_v1` の schema バリデーション失敗 | request_validation | `schema must equal delegation_request_v1` | none |
| `request_policy_denied` | tool_profile ポリシー違反（`proposal_only` での write 要求など） | request_validation | `proposal_only forbids direct file write/edit requests` | none |
| `config_invalid` | model_routing YAML が不正 / default_chain が空 | runtime_preflight | `model_routing config error: ...` / `routing_config_invalid` | config_fix |
| `cli_missing` | `gemini` コマンドが見つからない | cli_process | `FileNotFoundError` / `command not found` | install_cli |
| `cli_incompatible` | `gemini --help` が required flags を欠いている | cli_process | `gemini --help is missing: --output-format, ...` | upgrade_cli |
| `trusted_workspace_required` | smoke test で trusted directory エラー検出 | cli_process | stderr: `trusted directory` / `GEMINI_CLI_TRUST_WORKSPACE` | set_trust_env |
| `auth_missing_or_expired` | OAuth トークン失効・認証未完了の明示的 signal（`not authenticated` / `UNAUTHENTICATED` / auth context 明確な `PERMISSION_DENIED`）| api_backend | stderr: `not authenticated` / `UNAUTHENTICATED` | reauth |
| `permission_denied` | 認証は有効だが権限不足（`PERMISSION_DENIED` かつ auth-expired signal なし） | api_backend | stderr: `PERMISSION_DENIED` without auth context | check_iam_permissions |
| `billing_or_region_unavailable` | 課金未設定 / free tier 上限 / リージョン制限（`FAILED_PRECONDITION` / `free tier unavailable`）| api_backend | `FAILED_PRECONDITION` / `free tier limit` / `billing required` | check_billing_or_region |
| `model_not_found_or_unsupported` | モデルが存在しないまたはサポート外（`NOT_FOUND` / `unsupported model`）| api_backend | `NOT_FOUND` / `model not found` / `unsupported model` | check_model_name |
| `gh_auth_required` | `github_research` で全 gh_commands が認証エラーで失敗 | github_preflight | `gh auth status` failed / `all gh_commands failed` | gh_auth_login |
| `mcp_config_invalid` | `local_asset_research` の Serena MCP 設定不正 | runtime_preflight | `local_asset_research requires .gemini/settings.json mcpServers.serena` | fix_mcp_config |
| `mcp_tool_policy_invalid` | includeTools に許可外ツールが含まれている（configuration hygiene check であり、security boundary ではない）| runtime_preflight | `local_asset_research includes dangerous Serena MCP tools` | fix_mcp_tool_policy |
| `github_research_command_denied` | `github_research` で禁止 gh subcommand が検出された | request_validation | `github_research_command_denied` / `is not in the allowed subcommand list` | none |
| `api_deadline_exceeded` | prompt / context が大きすぎて API deadline を超過（request 調整が必要）| api_backend | `DEADLINE_EXCEEDED` / `context length exceeded` / `prompt too large` | reduce_request_size |

### Retryable failures（再試行可能な失敗、backoff retry 可）

これらは一時的な状態変化で解消される可能性がある。
exponential backoff retry が有効。

| `failure_class` | 意味 | 発生レイヤー | Raw Signal 例 | `retry_scope` |
|---|---|---|---|---|
| `quota_or_rate_limited` | API quota / rate limit（RPM/TPM/RPD いずれか。`quota_dimension` で区別）| api_backend | HTTP 429 / `RESOURCE_EXHAUSTED` / `quota` / `rate limit` / `too many requests` | `same_request_after_backoff` または `next_model`（RPD 枯渇時） |
| `model_capacity_exhausted` | 特定モデルの処理キャパシティ不足（429 / capacity 系）、model downgrade で回復する場合がある | api_backend | `MODEL_CAPACITY_EXHAUSTED` / `model capacity` / HTTP 429 | `next_model` 優先 |
| `transient_api_error` | API バックエンドの一時障害（HTTP 500 / 503） | api_backend | HTTP 500 / HTTP 503 / `internal error` / `service unavailable` | `same_request_after_backoff` |
| `network_error` | ネットワーク到達不能・ソケットタイムアウト | cli_process | `connection refused` / `socket timeout` / `network unreachable` | `same_request_after_backoff` |
| `client_subprocess_timeout` | `timeout_sec` 超過による subprocess タイムアウト（プロセス stall / ネットワーク stall） | cli_process | `subprocess.TimeoutExpired` / exit code 124 | `same_request_after_backoff`（timeout_sec 拡大を要検討） |

### Terminal / exhausted failures（終端・枯渇状態の失敗）

retry budget 枯渇や model chain 全滅など、これ以上 retry しても意味がない状態。
Human escalation を推奨。

| `failure_class` | 意味 |
|---|---|
| `model_chain_exhausted` | model_chain 内の全モデルが quota / capacity で失敗し chain 全滅 |
| `retry_budget_exhausted` | `RETRY_LIMIT` 回の retry 後も同じ failure が継続 |
| `unknown_cli_failure` | non-zero exit code だが既知パターンにマッチしない |
| `unknown_api_error` | Gemini envelope に `error` オブジェクトが含まれるが既知分類不能 |

### AGY provider failure classes（AGY プロバイダの失敗分類、Issue #1270）

`provider=agy` の `_classify_agy_failure()`（`run_gemini_headless.py`）が
stdout / stderr の両方から判別する failure_class。`_normalize_agy_result()`
の non-zero exit 分岐がこの分類器を使う（以前は `agy_exit_nonzero` に
一律丸められていた）。

| `failure_class` | 意味 | retryable（provider fallback 対象） |
|---|---|---|
| `agy_rate_limited` | AGY 側の rate limit / quota 系エラー（`RESOURCE_EXHAUSTED` / `429` / `rate limit`） | yes |
| `agy_capacity_exhausted` | AGY 側のモデル capacity 不足（`MODEL_CAPACITY_EXHAUSTED` / `overloaded` / `UNAVAILABLE`） | yes |
| `agy_web_grounding_quota_exhausted` | grounded_research の web grounding quota 枯渇（`Individual quota reached` 等。既存 `preflight_agy.py` の `_QUOTA_EXHAUSTED_RE` と同じ検出対象を一般化） | yes |
| `agy_auth_required` | AGY 認証未完了 / 失効 | no |
| `agy_permission_denied` | AGY 権限不足（403 / forbidden、または `agy` exec 時の OS `PermissionError`。fix_delta Blocker 6: `run_delegation()` の `PermissionError` except 節は `_classify_agy_failure()` と同一のこのクラスへ正規化する） | no |
| `agy_not_found` | `agy` バイナリが `PATH` に見つからない（`run_delegation()` の `FileNotFoundError` except 節。terminal / non-retryable — `cli_missing` の AGY 版に相当） | no |
| `agy_timeout` | subprocess タイムアウト | no |
| `agy_exit_nonzero` | non-zero exit だが既知の quota/auth/permission signal にマッチしない一般失敗 | no |
| `agy_empty_stdout` | 非 CI 環境で exit 0 だが stdout が空 | no |
| `agy_output_missing` | CI 環境で exit 0 だが stdout が空（`agy_empty_stdout` と同一原因、CI 判定のみ異なる。#1274: `warnings[0]` の leading token は必ず `failure_class` と一致させる） | no |
| `agy_unexpected_error` | AGY 実行時の未分類例外（terminal / non-retryable） | no |
| `agy_invocation_policy_denied` | agy 実行用 argv が位置ベースの構造 allowlist （`_validate_agy_invocation_argv()`、Issue #1807）に違反（`--dangerously-skip-permissions` 等の permission-bypass flag を含む未知の trailing option 混入等）。`agy_permission_denied`（AGY 側/OS レベルの権限拒否）とは異なり、wrapper 側が `subprocess.run()` 呼び出し前にfail-closed で拒否したことを示す。retryable: false | no |

### AGY permission-boundary runner failure classes（実行時の失敗分類、Issue #1814）

この二つは provider fallback の入力ではない。専用 runner は fallback provider を
起動せず、pytest skip やモデルの自己申告でこれらを成功へ置換しない。

| `failure_class` | runner exit | completion | retry / recovery |
|---|---:|---|---|
| `agy_permission_boundary_unavailable` | 77 | false | binary、既存 auth、または required runtime capability が unavailable。artifact は schema-valid で `actual_agy_executed: false` を記録し、外部状態が復旧した後に dedicated runner を再実行する。|
| `agy_permission_boundary_inconclusive` | 1 | false | injected attempt 不在、attempt correlation 不成立、hook lifecycle evidence 不足、または artifact invalid。実装または runtime evidence を修復して再実行する。|

predicate violation、unexpected `PostToolUse`、または side-effect counter の増加も
exit 1 / `completion: false` とする。fallback provider が成功してもこの分類を
上書きしてはならない。

runner-local の file mode / readback check は fail-closed local guardrail であり、
child に対する immutable authority boundary や secrecy を保証しない。artifact の
attempt correlation は parent runner が記録し、child hook の event は correlation
authority として扱わない。

#### `agy_permission_boundary_inconclusive` の根本原因（Issue #1814 再調査）

過去の live run が一貫して `agy_permission_boundary_inconclusive`（exit 1）を返していた
根本原因を、実インストール済み `agy`（Antigravity CLI 1.1.9、`~/.local/bin/agy`。
`@google/gemini-cli` npm パッケージとは別の Google 内製バイナリであり、`strings` で
抽出できる同バイナリ埋め込みの hooks リファレンス文書が正本）に対する直接的な
再現実験で特定した。二つの独立した原因が存在する。

**原因 1（この PR で修正済み）: `workspacePaths` が常に空になる欠落 `--add-dir`。**

`_invoke()` は `cwd=runtime["workspace"]` で `agy --print ...` を起動していたが、
`--add-dir <workspace>` を渡していなかった。実 AGY の hook 共通入力フィールド
`workspacePaths` は `cwd` からではなく `--add-dir` で明示的に登録した
workspace のリストからのみ生成される（`cwd` だけを設定した再現実験では
`"workspacePaths":[]`、`--add-dir` を追加すると
`"workspacePaths":["<workspace>"]` に変わることを直接確認した）。
`_prepare_runtime()` の `PreInvocation` injection hook は
`context['workspace'] in payload.get('workspacePaths', [])` を要求するため、
`--add-dir` 欠落時はこの workspace-binding check が常に失敗し、
`injectSteps` が一度も受理されず、結果として `PreToolUse` イベントが
一件も発生しない。これが過去のすべての live run で
`diagnostic_ledger.pre_invocation_context_accepted` が `false` になっていた
直接の原因であり、`run_agy_permission_boundary_e2e.py` の `_invoke()` に
`--add-dir` を追加することで解消した（`diagnostic_ledger.pre_invocation_context_accepted`
は `true` に変わることを live run で確認済み）。

**原因 2（未解決。live exit 0 の contract/implementation mismatch として記録）:
`PreInvocation` の `injectSteps` の `toolCall` ステップ型が実バイナリで機能しない。**

原因 1 を修正した後も、live run は依然として exit 1 になる。これは
`PreInvocation` hook が返す `injectSteps` の `toolCall` ステップ
（`agy` 自身が埋め込んでいる hooks リファレンス文書に記載された、
`{"toolCall": {"name": "...", "args": {...}}}` という契約どおりの形）を
実バイナリが受理しないためである。この PR とは独立した最小 hooks.json
だけを使う再現実験で、以下を確認した。

- `PreInvocation` / `PreToolUse` hook 自体は正しく発火し、実際のモデルが
  発行した `view_file` tool call を `PreToolUse` の `{"decision": "deny", "reason": "..."}`
  で確実に deny できる（モデル応答に
  `tool call denied with reason: probe deny` が反映され、実行はされなかった）。
  つまり `PreToolUse` deny 境界そのものは実 runtime で機能する。
- `injectSteps` の `{"ephemeralMessage": "..."}` ステップは受理され、
  agy は exit 0 で完了する（camelCase / snake_case のどちらの outer key
  `injectSteps` / `inject_steps` でも同様に受理された）。
- `injectSteps` の `{"toolCall": {"name": "...", "args": {...}}}` ステップ
  （camelCase / snake_case のどちらの key 組み合わせでも）は agy 自身の
  `--log-file` 出力に
  `error in pre-invocation hook: failed to inject steps from hook ...:
  unknown injected step type: <nil>` を出力して agent 実行全体を
  `Error: Agent execution terminated due to error.`（exit 1）で
  中断させる。permission 設定を完全に外した（`permissions` 制約なしの
  素の isolated HOME）状態でも同一のエラーになるため、これは
  permission boundary の deny ではなく、`toolCall` ステップの
  デシリアライズに関する実装側の欠陥または同バイナリの埋め込み文書との
  version skew である。
- `{"type": "toolCall", "toolCall": {...}}` のように discriminator を
  追加すると、エラーメッセージ自体が変化し
  (`unmarshal result ... via protojson: ... unknown field "type"`)、
  hook 結果は protojson で厳格にデコードされることが分かる。すなわち
  `toolCall` という field 名自体は protojson レベルでは受理される
  （`unknown field` エラーにならない）にもかかわらず、その後段の
  アプリケーションコード（`executor.go`）側で "unknown injected step
  type: <nil>" として扱われる。これはクライアント側の JSON key
  ケーシングの誤りではなく、実バイナリ内部の型解決ロジック側の
  問題であることを強く示唆する。

**追加の独立確認（Issue #1814 再々調査、current head `ffd0fb83`）。**
コード読み取りのみ（`strings ~/.local/bin/agy`、追加の live 起動なし）で
上記の結論を補強する事実を確認した。埋め込み `hooks_go_proto.HookInjectedStep`
の oneof フィールド `Step` が取りうる型は、シンボルテーブルから
`HookInjectedStep_ToolCall` / `HookInjectedStep_UserMessage` /
`HookInjectedStep_EphemeralMessage` / `HookInjectedStep_ErrorMessage` /
`HookInjectedStep_SystemMessage` / `HookInjectedStep_HookUserMessage` /
`HookInjectedStep_HookEphemeralMessage` の 7 種類だけであり、
`HookInjectedStep_HookToolCall`（`HookUserMessage` / `HookEphemeralMessage`
と対になる、新しい `HookToolCall` message 型を包む oneof variant）は
**存在しない**。つまり `toolCall` を注入するために protojson が受理しうる
JSON key は `toolCall`（または proto 原名の `tool_call`）以外に存在せず、
この形は既に確認済みで `unknown injected step type: <nil>` になる。
また `{"type": "toolCall", ...}` のような discriminator 付与は
`HookInjectedStep` に `type` という宣言フィールドが存在しないため
protojson の unknown-field 拒否（`unknown field "type"`）に一致する。
`type` の値を `tool_call` / `TOOL_CALL` に変えても、拒否理由は
フィールド名 `type` 自体の不存在であって値ではないため、同一の
`unknown field "type"` エラーになることが構造的に導ける。したがって
`--profile no_tools --mode live --allow-live` による追加の live 起動を
伴わずに、これらの discriminator variant は同じ結果になると判断できる。

current head `ffd0fb83` に対して `--profile no_tools --mode live
--allow-live` を再実行し、`_prepare_runtime()` の `injectSteps` 構築
（`args` に `stepIndex` / `sideEffectCounterPath` を追加した correlation
強化後の形）でも同一の failure_class になることを再確認した。
artifact の `diagnostic_ledger` は
`pre_invocation_hook_started: true`、`pre_invocation_context_accepted: true`、
`injected_step_count: 5`（`PreInvocation` hook 自体は正しく発火し
5 attempt すべてが `injectSteps` として受理された）に対し、
`pre_tool_use_event_count: 0`（`PreToolUse` は一件も発火しなかった）、
`runner.child_returncode: 1`（`agy` 本体が deserialize 失敗で
エージェント実行全体を中断した）であった。`failure_taxonomy.class` は
`agy_permission_boundary_inconclusive`、`completion: false`。この結果は
原因 2 が correlation 強化コミット後も未解消であることを示す。

**この Issue の Stop Condition への該当性。** 上記は
「実 AGY の仕様が公式資料と runtime で矛盾し、fail-closed な判定を確定できない」
に該当する。`agy` 自身が埋め込む公式ドキュメントどおりの `toolCall` 注入
契約が実バイナリで機能しないことをクライアント側の再現実験で確認したが、
Google 非公開の内製バイナリ（`google3/third_party/jetski/...`）であるため、
ソースコードにアクセスできないこの環境から追加の JSON エンコーディングを
機械的に探索し続けても収束する保証がない。したがって AC3/AC5 が要求する
「`PreInvocation` の `injectSteps` による、モデル選択に依存しない決定論的
5-capability 注入」は、この agy ビルドでは現状確認できていない。人間判断
として以下のいずれかを選択する必要がある。

1. Google / Antigravity サポート経路で正しい `toolCall` 注入 JSON 形式を
   確認し、判明した形式で再検証する。
2. `injectSteps` の `toolCall` 注入に依存しない代替のデターミニズム設計
   （例: `PreToolUse` deny 自体は live で実証済みなので、モデルへの
   明示的・一意な単一 tool 呼び出し指示プロンプトと厳格な
   correlation/deny 検証を組み合わせる設計）へ、Issue の Out of Scope
   条項（「モデルが任意に tool を選択することだけに依存する E2E」の禁止）
   と両立する形で contract を改訂する。
3. AC3/AC5 を model capability 待ちの follow-up Issue へ切り出し、
   本 Issue は `PreToolUse` deny 境界の live 実証（原因 1 の修正と
   diagnostic_ledger 改善）までをスコープとして再定義する。

いずれも Issue 契約の変更を伴うため、この PR は Draft のまま
`Refs #1814` を維持し、`Closes #1814` を使用しない。

### 原因 2 への回避策（Issue #1979、2026-08-04 契約改訂）

squne121 の指示により、上記の選択肢 2（`toolCall` 注入に依存しない代替設計）
を採用した。`PreInvocation` hook の injectSteps を `toolCall`
（`{"toolCall": {"name": "...", "args": {...}}}`、原因 2 で機能しないことを
確認済み）から `ephemeralMessage`（`{"ephemeralMessage": "<自然言語指示>"}`、
上記の再現実験で受理されることを既に確認済み）へ切り替え、capability ごとに
最大 3 回の bounded retry で `PreToolUse` イベントの観測有無を確認する設計
（`run_agy_permission_boundary_e2e.py::_resolve_prompt_compliance()`）へ
転換した。bounded retry を使い切っても compliant にならない capability が
1 つでもあれば、新設の `EXIT_PROMPT_NONCOMPLIANT`(78) で終了し、通常の
allow/deny 判定へは進まない（`run_agy_permission_boundary_e2e.py::EXIT_PROMPT_NONCOMPLIANT`
/ `FAILURE_PROMPT_NONCOMPLIANT`、詳細は `docs/dev/schema-governance.md` の
Issue #1979 Compatibility Decision を参照）。

**2026-08-04 の live 検証結果（実 AGY 1.1.9、`--allow-live`、`allow`/`deny`
両 profile）。** `ephemeralMessage` 注入方式は実際に機能した。`PreInvocation`
hook は正しく発火し（`pre_invocation_hook_started: true` /
`pre_invocation_context_accepted: true`）、`command`/`read`/`network` の
3 capability は 1 ラウンド目で compliant になった（`child_returncode: 0`、
`actual_agy_executed: true`）。`write` capability のみ、3 ラウンドすべてで
対応する `PreToolUse` が一度も観測されず `prompt_noncompliance` となり、
両 profile とも `EXIT_PROMPT_NONCOMPLIANT`(78) で終了した（捏造した exit 0
ではない、genuine な非completion結果）。この時点では上記 77 行目で既述の通り
`write_to_file` の正式な引数キー名（`TargetFile`/`Overwrite`/`CodeContent`）
が一次資料で独立確認できておらず、この capability 固有の non-compliance の
有力な原因候補と見られていた。

**その後の追加修正で `write` capability の genuine `exit 0` に到達した。**
live 投機的プローブ（`write_to_file` PreToolUse matcher への stdin tee）で、
実 AGY の `write_to_file` ツール呼び出しには常に `Description` 引数が
含まれることを確認し、`attempt_args["write"]` に追加した。これにより
`args_digest` の完全一致判定が正しく成立するようになり、`write` capability も
両 profile でラウンド 1 から compliant になった（`EXIT_PROMPT_NONCOMPLIANT`
は解消）。

**残っていた別原因（deny 期待 attempt での PostToolUse 発火）も解消した。**
実 AGY は `PreToolUse` が正しく deny した後も、相関する `PostToolUse`
イベントを発火させることがある。旧来の `post_tool_use_matches_expectation`
/ `same_attempt_correlation` predicate はこれを mismatch
（`agy_permission_boundary_inconclusive`）として扱っていたため、genuine な
`exit 0` に到達できなかった。Issue #1979 の In Scope が規定する
`characterize_and_record`（explicit deny・同一 attempt への correlation・
secret 非開示を評価し、PostToolUse ゼロ固定を要求しない）を実装し、schema に
additive optional field `deny_post_tool_use_characterization` を追加した。
stray（相関しない）PostToolUse や secret 開示を伴う PostToolUse は引き続き
失敗として扱う（詳細は `docs/dev/schema-governance.md` の Issue #1979
Compatibility Decision を参照）。

**最後に発覚した loopback canary の shutdown race も修正した。** allow
profile（`grounded_research`）でのみ、実際に network hit が発生した後の
`_LoopbackCanary.stop()` が `socketserver.ThreadingMixIn` の既定
`block_on_close=True` による per-request handler thread への無期限 `join()`
で不定期にハングし得ることが判明した（deny profile は per-request thread が
生成されないため常に無害だった）。`block_on_close=False`・
`daemon_threads=True`・handler 側 socket read の bounded timeout を追加して
解消した。

**最終確認（実 AGY 1.1.10、`--allow-live`）。** 上記のすべての修正
（`Description` 引数追加・deny 時 PostToolUse の characterize 化・loopback
shutdown race 修正）を適用した結果、`command`/`write`/`read`/`network` の
4 capability 全てが両 profile でラウンド 1 から compliant になり、
`grounded_research`（AC3, allow）・`no_tools`（AC4, deny）の両 profile が
繰り返し（AC3 は独立 3 回、AC4 は独立 2 回、クリーンな artifact-dir）で
genuine `exit 0` に到達することを確認した（捏造した exit 0 ではなく、
`cleanup` 全フィールド `true`、`failure_taxonomy.completion == true`）。
本 Issue の Delivery Rule（両 profile で `exit 0` が得られること）を
満たしたため、この PR は `Closes #1979` を使用する。

### provider_auto_policy_v1 fallback classes（フォールバック分類、Issue #1270）

`provider=auto`（`provider_auto_dispatch()`）が provider fallback の
可否判断・停止理由に使う top-level クラス。`provider_auto_policy_v1`
の `retryable_failure_classes` / `stop_if` に対応する（
`config/model_routing.yaml` 参照）。

| `failure_class` / `fallback_reason` token | 意味 | fallback 可否 |
|---|---|---|
| `quota_or_rate_limited` | Gemini 側の quota/rate-limit（provider fallback 対象） | yes（次 provider へ） |
| `model_capacity_exhausted` | Gemini 側の単一モデル capacity 不足（同一 provider 内 model downgrade で先に処理される） | yes（chain 全滅なら次 provider へ） |
| `model_chain_exhausted` | Gemini の model_chain 全滅（provider fallback の主要トリガー） | yes（次 provider へ） |
| `provider_profile_unsupported` | `tool_profile` が `provider_auto_policy_v1.eligible_profiles`（v1: `no_tools` / `proposal_only`）外 | no（dispatch 自体を行わない） |
| `provider_fallback_exhausted` | `runtime_order` の全 provider が retryable failure_class で失敗した（これ以上 fallback 先がない） | no（terminal） |

**Gemini / AGY / canonical class 対応表（正規クラス対応表）**

| 概念 | Gemini 側 | AGY 側 |
|---|---|---|
| quota / rate limit | `quota_or_rate_limited` | `agy_rate_limited` |
| model capacity 不足 | `model_capacity_exhausted` | `agy_capacity_exhausted` |
| chain / provider 全滅 | `model_chain_exhausted` | (該当なし。AGY は単一 model のため provider fallback がそのまま終端) |
| web grounding quota | (該当なし。web grounding は AGY grounded_research 専用) | `agy_web_grounding_quota_exhausted` |
| 認証失効 | `auth_missing_or_expired` | `agy_auth_required` |
| 権限不足 | `permission_denied` | `agy_permission_denied` |

`post_to_issue_url` を含む request、認証/権限/schema/policy 失敗、
`provider_profile_unsupported` はいずれも provider fallback の
stop condition であり、上記の「fallback 可否: no」に対応する
（`run_gemini_headless.py` の `provider_auto_dispatch()` 参照）。

### Conditionally retryable（条件付きで再試行可能）

状況依存で retry 可否が変わるクラス。

| `failure_class` | 意味 | retry 方針 |
|---|---|---|
| `output_parse_error` | Gemini CLI の JSON 出力が parse できない | 最大 1 回まで retry。CLI version incompatibility / stdout-stderr 混線の場合は retry 不可なので `classification_confidence: low` + human escalation |
| `empty_response` | `response_text` が空（API 呼び出し自体は成功） | 最大 1 回まで retry |

### ACP transport failure classes（ACP トランスポートの失敗分類、`transport_details.failure_class`）

ACP transport の failure は `transport_details.failure_class` に格納され、
headless_json fallback が可能なものと不可なものが区別されている（`transport-acp.md` 参照）。

| `failure_class` | fallback 可否 | 意味 |
|---|---|---|
| `gemini_not_found` | yes | `gemini --acp` 起動で FileNotFoundError |
| `launch_failed` | yes | subprocess 起動エラー |
| `initialize_failed` | yes | initialize timeout / エラー |
| `session_new_failed` | yes | session/new の non-auth エラー |
| `auth_required` | **no** | session/new で認証要求が検出（fail-close で surface） |
| `prompt_error` | no | session/prompt がエラー応答 |
| `protocol_error` | no | final response 前に EOF / process death |
| `incomplete_response` | no | stopReason が end_turn でない / empty response |
| `timeout` | no | total timeout 超過 |
| `watchdog` | no | HeartbeatWatchdog によるトリップ |
| `contract_bypass` | no | `prepared_prompt` なしで `run_acp()` 呼び出し |

### Serena MCP live collector failure classes（local_asset_research route の実ライブ収集失敗分類、Issue #2015）

`local_asset_research` route の `_collect_live_serena_read_only_evidence()`
（`run_gemini_headless.py`）が起こす stage-specific failure。専用の
`SerenaCollectorError` サブクラスとして送出され、`failure_class` 属性を
持つ。呼び出し元は失敗時に `delegation_result/v1.local_asset_retrieval_metadata`
の `stage_failure_class` へこの値を記録する（top-level
`failure_class` は既存 `local_asset_research live_serena_mcp_failed`
のまま固定 -- #277 が横断的に taxonomy を統合するまでの互換性維持）。

| `failure_class` | 意味 | retryable | `manifest_drift_failed` |
|---|---|---|---|
| `startup_timeout` | `initialize` / `tools/list` によるプロトコルネゴシエーションが session deadline 内に応答しなかった | yes（fresh process で最大1回） | false |
| `request_timeout` | `tools/call`（`find_file` / `search_for_pattern` / `get_symbols_overview`）が session deadline 内に応答しなかった | yes（fresh process で最大1回） | false |
| `process_exit` | Serena MCP subprocess が応答前に終了した | no | false |
| `protocol_error` | stdout に JSON-RPC として parse できない行が出現した（stderr との混線ではなく、stdout 自体のプロトコル違反） | no | false |
| `jsonrpc_error` | サーバーが JSON-RPC `error` オブジェクトを返した | no | false |
| `manifest_drift` | `tools/list` が checked-in manifest（`read_only_allowlist` / `known_tools`）と一致しない。この class のみ `manifest_drift_failed: true` を設定する | no | **true** |
| `redaction_failure` | tool 結果に credential-like な文字列が検出された | no | false |
| `cleanup_failure` | subprocess（またはその descendant）の termination 後の reap に失敗した | no | false |

retry policy（Issue #2015 AC5）: `startup_timeout` / `request_timeout` の
みが対象。fresh process で最大1回まで retry し、`initial_result` を破棄
せず `initial_failure_class` として記録する（silent retry・無制限 retry
は禁止）。他の failure class は retry せず即座に fail-close する。

deadline hierarchy（Issue #2015 AC6、`time.monotonic()` ベース）:
内側の呼び出しほど短い制限時間を持つよう、次の順で厳密に大きくなる階層関係を維持する。
`server_tool_timeout`（45s）< `client_request_timeout`（60s）<
`collector_session_deadline`（120s）< `route_harness_timeout`（180s、
`scripts/agent-ops/run_agent_provider_route_smoke.py --timeout-seconds`
既定値）- `cleanup_grace`（10s）。外側の制限が内側の制限より必ず大きいことで、
タイムアウト発生時に内側から順に安全に打ち切られる。

request ledger（Issue #2015 AC1）: 各 JSON-RPC request は
`local_asset_retrieval_metadata.request_ledger` に
`request_id` / `method` / `tool_name` / `arguments_sha256` /
`started_at_monotonic` / `elapsed_sec` / `response_received` / `error`
を持つエントリとして記録される。`request_id` の発行順は
`initialize`=1 → `tools/list`=2 → `find_file`=3 →
`search_for_pattern`=4 → `get_symbols_overview`=5（`notifications/initialized`
は notification のため id を消費しない）。

---

## result JSON フィールド仕様（AC2 対応）

### `gemini_headless_preflight_result/v1` への追加フィールド

```yaml
failure_class:
  type: string | null
  nullable: true
  meaning: "最も具体的な失敗分類。成功時は null。"
  values: [上記 taxonomy の全値]

retryable:
  type: boolean
  meaning: "caller がこの failure に対して同一リクエストで retry を試みてよいか。"
  note: "retry_scope も合わせて確認すること。retryable=false の場合は必ず retry_scope: none。"

retry_scope:
  type: string | null
  nullable: true
  values:
    - none                          # retry 不可（fail-close）。retryable=false 時に使用
    - same_model                    # 同一モデルで即時 retry
    - next_model                    # model_chain の次モデルへ downgrade
    - same_request_after_backoff    # exponential backoff 後に同一リクエストで retry
  note: >
    retryable=false の場合は必ず none。
    外部状態変化（auth 修正 / config 修正）による回復経路は
    recovery_scope / recovery_action フィールドで表現する。

recovery_scope:
  type: string | null
  nullable: true
  meaning: "外部状態変化による回復経路（retryable=false の場合に使用）"
  values:
    - none                  # 回復経路なし（request 自体が不正）
    - reauth                # OAuth 再認証
    - gh_auth_login         # gh auth login
    - config_fix            # model_routing YAML / settings.json の修正
    - install_cli           # gemini CLI のインストール
    - upgrade_cli           # gemini CLI のアップグレード
    - set_trust_env         # GEMINI_CLI_TRUST_WORKSPACE 設定
    - fix_mcp_config        # .gemini/settings.json mcpServers 修正
    - fix_mcp_tool_policy   # includeTools の許可外ツール削除
    - check_iam_permissions # IAM 権限確認
    - check_billing_or_region  # 課金設定 / リージョン確認
    - check_model_name      # モデル名の確認
    - reduce_request_size   # prompt / context サイズの削減

recovery_action:
  type: string | null
  nullable: true
  meaning: "recovery_scope の具体的な推奨アクション（人間向け自由記述）"
  example: "Run: gemini auth login"

attempts:
  type: int
  meaning: >
    preflight における smoke test の retry 回数のみカウント（初回試行 = 1）。
    run wrapper と合算しない。
    preflight_checks 構造体が導入された場合は各チェックの個別 attempt は
    preflight_checks[*].attempts に記録し、top-level は smoke retry 回数のみとする。
  example: 1

preflight_checks:
  type: object | null
  nullable: true
  meaning: >
    各 preflight チェックの個別結果。#101（per-profile 化）完了後に
    section-local classification と組み合わせて段階的に拡充する。
  structure:
    gemini_version:
      ok: boolean
      failure_class: string | null
    gemini_help:
      ok: boolean
      failure_class: string | null
    smoke:
      ok: boolean
      failure_class: string | null
    gh_cli:
      ok: boolean
      failure_class: string | null

last_error_summary:
  type: string | null
  nullable: true
  constraints:
    max_chars: 240
    redact:
      - API keys（gho_, github_pat_, sk-, Bearer トークン等）
      - OAuth access tokens
      - absolute home paths（可能な範囲で）
  meaning: >
    最後に発生したエラーの要約（240 文字以下、機密情報は redact 済み）。
    caller-facing canonical フィールド。
    source フィールドで出力元を区別する。
  source:
    type: string | null
    nullable: true
    values:
      - stderr         # subprocess stderr
      - stdout         # subprocess stdout
      - envelope.error # Gemini JSON envelope の error フィールド
      - exception      # Python 例外メッセージ
      - gh_stderr      # gh CLI の stderr

last_stderr_summary:
  type: string | null
  nullable: true
  meaning: >
    最後の subprocess 実行の stderr（先頭 240 文字。機密情報は redact 済み）。
    last_error_summary の auxiliary フィールド。source=stderr の場合と同値になる。
  constraints:
    max_chars: 240
    redact:
      - API keys（gho_, github_pat_, sk-, Bearer トークン等）
      - OAuth access tokens
      - absolute home paths（可能な範囲で）
```

### `delegation_result/v1` への追加フィールド

```yaml
failure_class:
  type: string | null
  nullable: true
  meaning: "最も具体的な失敗分類。成功時は null。"

failure_origin:
  type: string | null
  nullable: true
  values:
    - request_validation    # schema / policy バリデーション失敗
    - runtime_preflight     # CLI / config / MCP 設定チェック失敗
    - cli_process           # subprocess 起動・タイムアウト・exit 非 0
    - api_backend           # Gemini API エラー（quota, transient, auth）
    - output_contract       # JSON parse / empty response
    - github_preflight      # gh_commands 実行失敗
    - acp_transport         # ACP transport 固有エラー
    - post_processing       # post_to_issue_url など後処理失敗

retryable:
  type: boolean
  meaning: "caller が retry を試みてよいか"
  note: "retryable=false の場合は必ず retry_scope: none。"

retry_scope:
  type: string | null
  nullable: true
  values: [none, same_model, next_model, same_request_after_backoff]
  note: >
    retryable=false の場合は必ず none。
    外部修復経路は recovery_scope / recovery_action で表現する。

recovery_scope:
  type: string | null
  nullable: true
  meaning: "外部状態変化による回復経路（retryable=false の場合に使用）"
  values:
    - none
    - reauth
    - gh_auth_login
    - config_fix
    - install_cli
    - upgrade_cli
    - set_trust_env
    - fix_mcp_config
    - fix_mcp_tool_policy
    - check_iam_permissions
    - check_billing_or_region
    - check_model_name
    - reduce_request_size

recovery_action:
  type: string | null
  nullable: true
  meaning: "recovery_scope の具体的な推奨アクション（人間向け自由記述）"

quota_dimension:
  type: string | null
  nullable: true
  meaning: >
    failure_class=quota_or_rate_limited 時に枯渇している quota の種別。
    RPD 枯渇の場合は retry_scope を next_model（別プール）にすること。
  values:
    - rpm            # Requests Per Minute
    - tpm            # Tokens Per Minute
    - rpd            # Requests Per Day（枯渇時は retry_scope: next_model）
    - spend          # 課金上限 / budget exceeded
    - model_capacity # モデル処理キャパシティ（capacity 系 429）
    - unknown        # 種別不明

retry_after_ms:
  type: int | null
  nullable: true
  meaning: >
    failure_class=quota_or_rate_limited 時に API が返した retry-after ヒント（ミリ秒）。
    API が値を返さない場合は null。backoff 計算の参考値として使用する。

attempts:
  type: int
  meaning: "Gemini CLI subprocess の総起動回数（全モデル・全 retry を合算）"

attempts_by_model:
  type: list
  nullable: true
  item:
    model: string
    attempts: int
    final_failure_class: string | null
  example:
    - model: gemini-3-flash-preview
      attempts: 3
      final_failure_class: quota_or_rate_limited
    - model: gemini-2.5-flash
      attempts: 1
      final_failure_class: null   # 成功

last_error_summary:
  type: string | null
  nullable: true
  constraints:
    max_chars: 240
    redact: [API keys, OAuth tokens, absolute home paths]
  meaning: >
    最後に発生したエラーの要約（caller-facing canonical フィールド）。
    source フィールドで出力元（stderr/stdout/envelope.error/exception/gh_stderr）を区別する。
  source:
    type: string | null
    nullable: true
    values: [stderr, stdout, envelope.error, exception, gh_stderr]

last_stderr_summary:
  type: string | null
  nullable: true
  constraints:
    max_chars: 240
    redact: [API keys, OAuth tokens, absolute home paths]
  meaning: >
    最後の subprocess 実行の stderr（先頭 240 文字。機密情報は redact 済み）。
    last_error_summary の auxiliary フィールド。

classification_confidence:
  type: string
  values: [high, medium, low]
  meaning: >
    high: 既知の raw signal パターンに明確マッチ。
    medium: 間接的な推定。
    low: unknown / 推測が含まれる。human escalation 推奨。
```

---

## Retry Policy（AC3 対応）

### 基本方針

1. **fail-close group**（Non-retryable）: retry 一切不可。即時 fail-close して caller に返す。
   `retryable: false`、`retry_scope: none` を設定する。
   外部修復経路は `recovery_scope` / `recovery_action` で表現する。
   - 対象: `request_schema_invalid`, `request_policy_denied`, `config_invalid`,
     `cli_missing`, `cli_incompatible`, `trusted_workspace_required`,
     `auth_missing_or_expired`, `permission_denied`, `billing_or_region_unavailable`,
     `model_not_found_or_unsupported`, `gh_auth_required`, `mcp_config_invalid`,
     `mcp_tool_policy_invalid`, `github_research_command_denied`, `api_deadline_exceeded`

2. **backoff retry group**（Retryable）: exponential backoff retry 可。
   - 対象: `quota_or_rate_limited`, `model_capacity_exhausted`,
     `transient_api_error`, `network_error`, `client_subprocess_timeout`
   - 実装（Issue #1270 fix_delta Blocker 1）: `config/model_routing.yaml` の
     `providers.gemini.retry_budget`（`get_retry_budget()`）が同一モデルの
     試行回数（`same_model_attempts`）と backoff（`initial_backoff_seconds` /
     `max_backoff_seconds` / `jitter`）を決定する。`RETRY_LIMIT` は
     `retry_budget` 未設定時の default 生成にのみ使われるフォールバック定数。
   - `retryable_failure_classes` が同一モデル retry の可否を決定する（
     attempt 単位で `_classify_gemini_retry_failure_class()` が分類し、
     このリストに含まれる場合のみ retry する）。
   - `quota_or_rate_limited` で `quota_dimension: rpd` の場合は `retry_scope: next_model`
   - quota / capacity exhaustion 時は model downgrade（`retry_scope: next_model`）
   - `retry_after_ms` が設定されている場合は API hint を優先する

3. **conditional retry group**: `output_parse_error`, `empty_response` は最大 1 回まで retry。
   `classification_confidence: low` の場合は human escalation を推奨。

### timeout の扱い

`timeout` は原因によって分類を分ける:

- **`client_subprocess_timeout`**: `timeout_sec` 超過による subprocess のタイムアウト
  （プロセス stall / ネットワーク stall）。`retryable: true`、`retry_scope: same_request_after_backoff`。
  `timeout_sec` の拡大も検討する。

- **`api_deadline_exceeded`**: prompt / context が大きすぎて API deadline を超過。
  `retryable: false`、`retry_scope: none`。`recovery_scope: reduce_request_size`。
  request 自体を調整しない限り同じ結果になる。

### ACP transport の特例

`auth_required` は ACP transport の fail-close failure であり、
headless_json へのフォールバックを**行わない**（`transport-acp.md` の設計による）。
これは `auth.ok:false` 的な「aggregate field によるサイレント誤分類」を防ぐための意図的設計。

### Raw signal → failure_class の対応表（fixture table）

以下は実装時のテスト fixture として使用する。

| Raw signal | `failure_class` | `retryable` | `retry_scope` | `recovery_scope` | `quota_dimension` |
|---|---|---|---|---|---|
| `FileNotFoundError` on `gemini` launch | `cli_missing` | false | none | install_cli | - |
| `gemini --help` missing `--output-format` | `cli_incompatible` | false | none | upgrade_cli | - |
| stderr: `trusted directory` / `GEMINI_CLI_TRUST_WORKSPACE` | `trusted_workspace_required` | false | none | set_trust_env | - |
| HTTP 429 in stdout/stderr | `quota_or_rate_limited` | true | next_model | - | model_capacity |
| `RESOURCE_EXHAUSTED` in stdout/stderr | `quota_or_rate_limited` | true | same_request_after_backoff | - | unknown |
| `RESOURCE_EXHAUSTED` + `rpd` / `per day` context | `quota_or_rate_limited` | true | next_model | - | rpd |
| `MODEL_CAPACITY_EXHAUSTED` in stdout/stderr | `model_capacity_exhausted` | true | next_model | - | - |
| HTTP 500 in stdout/stderr | `transient_api_error` | true | same_request_after_backoff | - | - |
| HTTP 503 in stdout/stderr | `transient_api_error` | true | same_request_after_backoff | - | - |
| `subprocess.TimeoutExpired` | `client_subprocess_timeout` | true | same_request_after_backoff | - | - |
| exit code 124 | `client_subprocess_timeout` | true | same_request_after_backoff | - | - |
| `DEADLINE_EXCEEDED` / `context length exceeded` / `prompt too large` | `api_deadline_exceeded` | false | none | reduce_request_size | - |
| `socket timeout` / `connection refused` | `network_error` | true | same_request_after_backoff | - | - |
| `json.JSONDecodeError` on envelope | `output_parse_error` | true (max 1回) | same_model | - | - |
| `response_text` が空 / exit 0 | `empty_response` | true (max 1回) | same_model | - | - |
| model_routing YAML が invalid | `config_invalid` | false | none | config_fix | - |
| `all gh_commands failed` | `gh_auth_required` | false | none | gh_auth_login | - |
| `github_research_command_denied` | `github_research_command_denied` | false | none | none | - |
| `local_asset_research requires mcpServers.serena` | `mcp_config_invalid` | false | none | fix_mcp_config | - |
| `local_asset_research includes dangerous Serena MCP tools` | `mcp_tool_policy_invalid` | false | none | fix_mcp_tool_policy | - |
| `not authenticated` / `UNAUTHENTICATED` | `auth_missing_or_expired` | false | none | reauth | - |
| `PERMISSION_DENIED` with explicit auth context | `auth_missing_or_expired` | false | none | reauth | - |
| `PERMISSION_DENIED` without auth context | `permission_denied` | false | none | check_iam_permissions | - |
| `FAILED_PRECONDITION` / `free tier unavailable` / `billing required` | `billing_or_region_unavailable` | false | none | check_billing_or_region | - |
| `NOT_FOUND` / `model not found` / `unsupported model` | `model_not_found_or_unsupported` | false | none | check_model_name | - |

---

## #101 との依存関係

Issue #101 は preflight の per-profile 化を扱う。
現行 `preflight_gemini_headless.py` は `local_asset_research` の Serena 設定を
全 profile に対して検証して `failure_reason` に設定し即 return する問題がある（#101 未解決）。

本 taxonomy は以下の境界を採用する:

- **top-level `failure_class`**: preflight の全体成否を表す。#101 完了前は
  `local_asset_research` 関連の failure のみ `mcp_config_invalid` / `mcp_tool_policy_invalid` に分類。
  他プロファイルで Serena 設定が原因の誤 fail-close が発生した場合は `config_invalid` として扱い、
  #101 完了後に section-local classification に移行する。
- **section-local classification**: 各 section（`local_asset_research`, `gh_cli` 等）は
  `section.failure_class` として独立した failure_class を持つ（将来拡張）。
  top-level `failure_class` は最も重大な failure のみを反映する。

---

## 現行実装との差分（実装 Issue 起票時の参照用）

現行 `run_gemini_headless.py` / `preflight_gemini_headless.py` との主な差分:

1. **preflight の `failure_class`**: `trusted_workspace_required` のみ設定されている。
   `cli_missing`, `cli_incompatible`, `mcp_config_invalid`, `mcp_tool_policy_invalid`,
   `gh_auth_required` は未設定（`failure_reason` は設定されているが `failure_class` がない）。

2. **`retryable` フィールド**: 両スクリプトとも未実装。

3. **`attempts` フィールド**: `run_gemini_headless.py` は `RETRY_LIMIT = 2` の retry loop を
   実装しているが、result JSON に `attempts` を出力していない。
   `attempts_by_model` も未実装。

4. **`last_error_summary` / `last_stderr_summary` フィールド**: 未実装。
   `warnings` 経由で stderr が surfaced されているが、
   caller が読みやすい形式で `last_error_summary` を出力していない。

5. **`failure_class` の backoff retry group**: `_is_retryable_capacity_failure()` が
   `MODEL_CAPACITY_EXHAUSTED` / `RESOURCE_EXHAUSTED` / HTTP 429 を検出して
   retry しているが、result に `failure_class: quota_or_rate_limited` を設定していない。
   Model chain exhaustion 時は `reason_code: model_chain_exhausted` が設定されるが、
   `failure_class` は別フィールド。

6. **`timeout` の分割**: 現行は一律 `timeout` として扱っているが、
   `client_subprocess_timeout`（retryable）と `api_deadline_exceeded`（non-retryable）に分割が必要。

7. **`PERMISSION_DENIED` の分離**: 現行は `auth_missing_or_expired` に一括。
   `permission_denied` / `billing_or_region_unavailable` / `model_not_found_or_unsupported`
   への分類ロジックを追加する必要がある。

---

## 後続実装 Issue の分割方針

本 taxonomy を受けた実装は以下の 2 Issue に分割することを推奨する。

> **Issue #277 の scope 制限**:
> Issue A（run_gemini_headless.py 拡張）は本 taxonomy に基づき即時実装可能。
> Issue B（preflight_gemini_headless.py 拡張）のうち per-profile に関わる
> `preflight_checks` 構造の完全実装は **#101 解決後** に行う。
> #101 完了前は `preflight_checks` を optional な拡張として実装し、
> top-level `failure_class` のみを #101 未解決でも動作する範囲で実装すること。

### Issue A: `run_gemini_headless.py` の result schema 拡張

対象ファイル:
- `.claude/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py`
- `.claude/skills/gemini-cli-headless-delegation/tests/`

追加フィールド:
- `failure_class`（全クラスに対して正しく設定）
- `failure_origin`
- `retryable`（`retryable=false` の場合は必ず `retry_scope: none`）
- `retry_scope`
- `recovery_scope`
- `recovery_action`
- `quota_dimension`（`quota_or_rate_limited` 時）
- `retry_after_ms`（`quota_or_rate_limited` 時）
- `attempts`
- `attempts_by_model`
- `last_error_summary`（canonical）
- `last_stderr_summary`（auxiliary）

### Issue B: `preflight_gemini_headless.py` の result schema 拡張

対象ファイル:
- `.claude/skills/gemini-cli-headless-delegation/scripts/preflight_gemini_headless.py`
- `.claude/skills/gemini-cli-headless-delegation/tests/test_preflight_gemini_headless.py`

追加フィールド（既存 `failure_class: trusted_workspace_required` に加えて）:
- `cli_missing`
- `cli_incompatible`
- `mcp_config_invalid`
- `mcp_tool_policy_invalid`
- `gh_auth_required`
- `permission_denied`
- `billing_or_region_unavailable`
- `model_not_found_or_unsupported`
- `retryable`（`retryable=false` の場合は必ず `retry_scope: none`）
- `retry_scope`
- `recovery_scope`
- `recovery_action`
- `last_error_summary`（canonical）
- `last_stderr_summary`（auxiliary）
- `classification_confidence`
- `preflight_checks`（#101 完了後に per-profile 対応と組み合わせて拡充）

依存関係: #101 完了後に section-local failure_class への移行と `preflight_checks` の完全実装を行う。
