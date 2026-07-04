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
| `agy_permission_denied` | AGY 権限不足（403 / forbidden） | no |
| `agy_timeout` | subprocess タイムアウト | no |
| `agy_exit_nonzero` | non-zero exit だが既知の quota/auth/permission signal にマッチしない一般失敗 | no |
| `agy_empty_stdout` | 非 CI 環境で exit 0 だが stdout が空 | no |
| `agy_output_missing` | CI 環境で exit 0 だが stdout が空（`agy_empty_stdout` と同一原因、CI 判定のみ異なる。#1274: `warnings[0]` の leading token は必ず `failure_class` と一致させる） | no |

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
   - 既存実装: `RETRY_LIMIT = 2`、`time.sleep(min(2**attempt, 4))` で backoff
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
