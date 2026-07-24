# model-routing.md — モデルルーティング仕様

`run_gemini_headless.py` のモデル選択・自動降格チェーン機能の設計詳細。

## 概要

quota 枯渇（429 / `MODEL_CAPACITY_EXHAUSTED` / `RESOURCE_EXHAUSTED`）が発生した際、
wrapper は caller-side fallback（ClaudeCode 直接生成）の前に、設定で定義した下位モデルへ
自動的に降格 retry する経路を提供する。

## 設定スキーマ

### Python 定数 `DEFAULT_MODEL_ROUTING`（`run_gemini_headless.py` 内）

```python
DEFAULT_MODEL_ROUTING = {
    "default_chain": ["gemini-3-flash-preview", "gemini-2.5-flash"],
    "roles": {
        "code_research":   {"model_chain": ["gemini-3-flash-preview", "gemini-2.5-flash"]},
        "web_research":    {"model_chain": ["gemini-3-flash-preview", "gemini-2.5-flash"]},
        "github_research": {"model_chain": ["gemini-3-flash-preview", "gemini-2.5-flash"]},
        "implementation":  {"model_chain": ["gemini-3-pro-preview", "gemini-3-flash-preview", "gemini-2.5-flash"]},
        "issue_authoring": {"model_chain": ["gemini-3-pro-preview", "gemini-3-flash-preview", "gemini-2.5-flash"]},
    },
}
```

### オーバーライドファイル `config/model_routing.yaml`（任意）

ファイルが存在する場合、`DEFAULT_MODEL_ROUTING` に deep-merge される。
コードを変更せずにモデル ID を差し替える場合はこのファイルを編集すること。

```yaml
default_chain:
  - gemini-3-flash-preview
  - gemini-2.5-flash

roles:
  implementation:
    model_chain:
      - gemini-3-pro-preview
      - gemini-3-flash-preview
      - gemini-2.5-flash
```

**バリデーション規則**（違反時は `ValueError` で fail-closed → `reason_code: routing_config_invalid`）:
- `default_chain` は非空リスト
- 各 role の `model_chain` は非空リスト
- 不正な YAML は `yaml.YAMLError` → `ValueError` に変換
- YAML ファイルのトップレベルが mapping でない場合は `ValueError`

**PyYAML 未導入環境での動作**:
- PyYAML が未インストールかつ `config/model_routing.yaml` が存在する場合: `RuntimeWarning` を発行して YAML override を無視し、`DEFAULT_MODEL_ROUTING` を使用する（fail-closed しない）。
- config ファイルが壊れている（YAMLError / スキーマ不正 / 空 chain）場合は従来どおり `ValueError` で fail-closed（設定者の明確なミスのため）。

## Role テーブル

| role | 用途 | model_chain（デフォルト） |
|---|---|---|
| `web_research` | 外部 Web 調査・grounded_research | `["gemini-3-flash-preview", "gemini-2.5-flash"]` |
| `code_research` | コードベース調査・ローカル資産調査 | `["gemini-3-flash-preview", "gemini-2.5-flash"]` |
| `github_research` | GitHub read-only 調査 | `["gemini-3-flash-preview", "gemini-2.5-flash"]` |
| `implementation` | 実装提案・設計案下書き | `["gemini-3-pro-preview", "gemini-3-flash-preview", "gemini-2.5-flash"]` |
| `issue_authoring` | Issue 本文案・仕様記述下書き | `["gemini-3-pro-preview", "gemini-3-flash-preview", "gemini-2.5-flash"]` |

> **注意**: モデル ID は `DEFAULT_MODEL_ROUTING` 由来。公式安定版エイリアスが確定したら
> `config/model_routing.yaml` で差し替えること（コード変更不要）。

## Chain 解決順

```
resolve_model_chain(request, routing):
  1. request["model"] が明示指定されている
       → chain = [request["model"]]（単一・降格なし）
  2. elif request["role"] が設定されており既知
       → chain = roles[role]["model_chain"]
  3. else
       → chain = default_chain
```

**fail-closed 条件**:
- `role` が未知 → `reason_code: "unknown_role"`、空 chain を返す
- chain が空 → `reason_code: "empty_chain"`、空 chain を返す

## 実行フロー（quota 枯渇時）

> **legacy description（#1270 以前）**: 以下の疑似コードは Gemini provider 単体の
> model-chain 降格ループのみを説明する固定 `RETRY_LIMIT` 前提の初期実装であり、
> `providers[<provider>].retry_budget`（`get_retry_budget()`、Issue #1270 AC2）による
> provider ごとの試行回数/backoff 上書きや、`provider="auto"` の provider レベル
> フォールバック（下記「provider="auto" のフォールバック（`provider_auto_policy_v1`）」節）
> は反映していない。`RETRY_LIMIT + 1` は `DEFAULT_RETRY_BUDGET.same_model_attempts`
> のデフォルト値と一致するが、`model_routing.yaml` で provider ごとに上書き可能。
> 現行の完全な仕様は本セクション末尾の「provider="auto" のフォールバック」節と
> `run_gemini_headless.py` の `get_retry_budget()` / `provider_auto_dispatch()` を正本とする。

```
for model in chain:
  for attempt in range(RETRY_LIMIT + 1):
    result = run_gemini(model)
    if success: return result
    if quota_error:
      if attempt < RETRY_LIMIT: backoff(); continue
      else: mark quota_exhausted; break  # 同一 model retry 上限到達
    else: break  # 非 quota エラーはそのまま返す

  if quota_exhausted and 次 model あり:
    emit model_downgrade event → next model
    continue to next model
  else:
    break  # 成功 or 非 quota 失敗

if chain 使い切り:
  fail-closed: reason_code = "model_chain_exhausted"
```

## reason_code 一覧

| reason_code | 発生条件 | `ok` |
|---|---|---|
| `quota_model_downgrade` | 降格イベント単位（`model_downgrades` リスト内の `reason` フィールド） | — |
| `model_chain_exhausted` | chain 内すべての model が quota 枯渇で失敗 | `false` |
| `unknown_role` | request の `role` フィールドが `roles` マップに存在しない場合（`resolve_model_chain` 側） | `false` |
| `empty_chain` | chain が空（設定エラー） | `false` |
| `routing_config_invalid` | `model_routing` 設定の読込/検証失敗（不正 YAML / スキーマ不正 / 空 chain 等） | `false` |

## result JSON 追加フィールド

既存フィールドを削除・改名せず追加のみ:

| フィールド | 型 | 説明 |
|---|---|---|
| `model_chain` | `list[str]` | 実際に試行対象だった model のリスト（chain 全体） |
| `model_downgrades` | `list[{from, to, reason}]` | 降格イベントのリスト。降格なし時は `[]` |
| `actual_model` | `str` | 最終的に使用した model（成功時）または最後に試みた model（失敗時） |
| `reason_code` | `str` | fail-closed 理由コード（エラー時のみ設定） |

## model と role の関係

`role` は `tool_profile` と**独立した**概念:
- `tool_profile`: Gemini CLI に渡すツール許可セット（`no_tools` / `grounded_research` 等）を制御する。**必須フィールド**。
- `role`: quota 枯渇時の降格チェーン選択にのみ使用する。**任意フィールド**。

両フィールドは同時に指定可能（例: `tool_profile: "grounded_research"`, `role: "web_research"`）。

## caller-side fallback との優先順位

> **legacy description（#1270 以前）**: 下図は `provider="gemini"` 単体（provider
> フォールバックが存在しなかった頃）の優先順位のみを示す。`provider="auto"` を
> 指定した場合は、wrapper 内降格が尽きた provider の失敗が
> `PROVIDER_AUTO_RETRYABLE_FAILURE_CLASSES` に該当する retryable class であれば、
> caller-side fallback の前に **provider レベルのフォールバック**
> （`provider_auto_dispatch()`、次節）が挟まる。図の `chain_exhausted` は
> `PROVIDER_AUTO_RETRYABLE_FAILURE_CLASSES["gemini"]` に含まれる retryable な
> failure_class の一つであり、`provider="auto"` では次候補（`agy`）へのフォール
> バックを引き起こしうる。

```
wrapper 内降格（model_chain 試行） → 全 model 失敗 → chain_exhausted を返す
                                           ↓
                              caller-side fallback（ClaudeCode 直接生成）
```

wrapper が `ok: false` + `reason_code: "model_chain_exhausted"` を返した後に
caller（`web-researcher` 等）が ClaudeCode 直接生成 fallback を発動する
（`provider="gemini"` を明示指定した場合、または `provider="auto"` で
provider フォールバックも尽きた場合）。

## provider="auto" のフォールバック（`provider_auto_policy_v1`、Issue #1270）

`provider="auto"` は `PROVIDER_AUTO_RUNTIME_ORDER`（`("gemini", "agy")`）を
逐次試行するメタ provider である。各 provider は自身の retry_budget
（`get_retry_budget()`）を使い切った後にのみ次の provider へフォールバックし、
`PROVIDER_AUTO_RETRYABLE_FAILURE_CLASSES` に含まれない failure_class
（validation / auth / permission 等）は即座に fail-closed する（フォールバック
しない）。`tool_profile` が `PROVIDER_AUTO_ELIGIBLE_PROFILES` に含まれない場合、
または `post_to_issue_url` 指定後の2回目以降の provider 試行は、idempotency
guard により provider 試行自体を行わない。詳細は
`run_gemini_headless.py` の `provider_auto_dispatch()` / `PROVIDER_AUTO_*`
定数群を正本とする。

## model-policy subcommand の追加（`build_request.py model-policy`、Issue #1269）

`build_request.py` の `model-policy` サブコマンドは、provider・role・runtime が
実際に解決する model chain を **読み取り専用・副作用なし**で確認する dry-run
inspector である。`load_model_routing()` / `resolve_model_chain()` をそのまま
呼び出すだけで、YAML parsing・default merge・precedence をこのサブコマンド側
で再実装しない。`--profile` / `--objective` を要求する既存の legacy invocation
（request 生成）とは非破壊で共存し、`argv[0] == "model-policy"` のときのみ
専用 parser へ dispatch する。request file・output file は一切書き込まない。

### 使用例

```bash
# gemini + role: resolve_model_chain() の戻り値をそのまま stdout に出す
uv run python3 .claude/skills/gemini-cli-headless-delegation/scripts/build_request.py \
  model-policy --provider gemini --role implementation

# agy: actual_model は常に null。"agy-default" は legacy_compatibility_label としてのみ出力
uv run python3 .claude/skills/gemini-cli-headless-delegation/scripts/build_request.py \
  model-policy --provider agy

# auto: --profile が必須。provider_candidates / runtime_order / profile_eligible /
# consumer_constraints を出力する
uv run python3 .claude/skills/gemini-cli-headless-delegation/scripts/build_request.py \
  model-policy --provider auto --profile no_tools
```

### 制御フロー順序（Issue #1695 PR review Blocker 2）

`build_model_policy()` の分岐順序は `run_gemini_headless.py` 自身の dispatch
順序をそのまま鏡写しにする（このサブコマンド独自の順序を発明しない）:

1. `provider` を `MODEL_POLICY_PROVIDERS`（`gemini`/`agy`/`auto`）と照合する
   （CLI の `choices` を経由しない直接の Python API 呼び出しでも fail-closed
   するため）。
2. `provider="agy"` は**ここで即座に確定**し、`load_model_routing()` を
   一切呼び出さない。`_run_delegation_core()` が `provider="agy"` を Gemini の
   validation/routing より前に分岐させるのと同じ理由で、model_routing.yaml
   が壊れていても `--provider agy` の inspection は成功する。
3. `provider="auto"` は `--profile` の有無、次に
   `PROVIDER_AUTO_ELIGIBLE_PROFILES` への該当有無を、**どちらも routing 読込
   より前に**判定する。`provider_auto_dispatch()` が ineligible な
   `tool_profile` に対して provider 試行を一切行わない（＝ routing も読まない）
   のと同じ理由で、profile が ineligible な場合は model_routing.yaml が
   壊れていても、また未知の `--role` を指定していても inspection は成功し、
   `profile_eligible: false` のみを返す。
4. `provider="gemini"`、または `provider="auto"` で profile が eligible な
   場合のみ、`load_model_routing()` / `resolve_model_chain()` を呼び出す。

### 出力スキーマ `delegation_model_policy/v1`（discriminated union）

全 variant 共通のベースフィールド: `schema`（常に `"delegation_model_policy/v1"`）、
`provider` / `role` / `profile`（引数のエコー。`invalid_provider` 失敗時は
検証前の生値をそのままエコーする）、`ok`、`failure_class`、`failure_reason`
（成功時は両方 `null`）。以下、provider と結果ごとの discriminator と追加
フィールドを示す（`—` は「このフィールドは存在しない」、`null` は
「フィールドは存在するが値が null」）。

| provider | 結果 | 追加フィールド |
|---|---|---|
| （全 provider 共通） | `invalid_provider` | 追加なし（ベースのみ。`failure_class: "invalid_provider"`） |
| `gemini` | 成功 | `resolved_chain: list[string]`（非空）、`actual_model: null`、`resolver_source: "run_gemini_headless.resolve_model_chain"` |
| `gemini` | `unknown_role` / `empty_chain` | 追加なし（ベースのみ） |
| `gemini` | `config_invalid` | `reason_code: "routing_config_invalid"` |
| `agy` | 成功（常に `ok: true`） | `resolved_chain: null`、`configured_chain: null`、`actual_model: null`、`legacy_compatibility_label: "agy-default"`、`wrapper_capability: {explicit_model_selection, role_based_model_chain}`（両方 `false`）、`upstream_capability: {probed, documented_explicit_model_selection, installed_version, installed_version_probed, note}`、`readiness_checked: false`、`credentials_checked: false`、`provider_available: null`。`--role` 指定時のみ追加で `role_applied: false` / `role_note: <string>` |
| `auto` | `--profile` 省略 | 追加なし（ベースのみ。`failure_class: "profile_required_for_auto"`） |
| `auto` | profile が `PROVIDER_AUTO_ELIGIBLE_PROFILES` 外 | `runtime_order: list[string]`、`profile_eligible: false`、`provider_candidates: null`、`consumer_constraints: null`（`ok: true` — routing 未読込のまま「試行なし」を報告） |
| `auto` | `config_invalid` / `unknown_role` / `empty_chain`（profile eligible） | `reason_code`（`config_invalid` のみ）を除きベースのみ |
| `auto` | profile eligible・成功 | `runtime_order: list[string]`、`profile_eligible: true`、`provider_candidates: list[object]`（各要素は `provider` フィールドで discriminate — `gemini` 候補は `{provider, resolved_chain, actual_model}` の3キーのみ、`agy` 候補は上記 agy 成功 variant と同じキー集合）、`consumer_constraints: {fan_out: {supported: false, reason_code: string}, agy_fallback_requires_prompt: true, explicit_model_survives_fallback: false}` |

`resolved_chain` は「provider が実際に実行可能かどうか（readiness）」ではなく
「設定から解決された候補チェーン」を表す。実行可能性の live probe（readiness /
credentials / provider の実在確認）は本サブコマンドの scope 外であり、
`readiness_checked` / `credentials_checked` / `provider_available` は
それを明示するための常に静的な値（`false` / `false` / `null`）である。

### AGY / auto の capability 表示ルール（Blocker 4 / Blocker 6、Major 3）

- `--provider agy`: `run_delegation()` は `provider="agy"` に対して
  `resolve_model_chain()` を一切呼び出さない（AGY に model chain の概念が
  存在しない）ため、`resolved_chain` / `configured_chain` は常に `null`。
  実行時に返る `actual_model: "agy-default"`（リテラル固定値）は、
  model-policy の `actual_model` フィールドには**絶対に出力せず**、
  `legacy_compatibility_label` としてのみ表示する。`wrapper_capability`
  （`explicit_model_selection`: false、`role_based_model_chain`: false）と
  `upstream_capability` を分離して出力する。`upstream_capability` は
  「wrapper が upstream の明示的モデル選択サポートを文書化しているか」
  （`documented_explicit_model_selection`）と「インストール済み CLI の
  バージョンを probe したか」（`installed_version` / `installed_version_probed`）
  を区別する（両方とも本サブコマンドでは live probe しないため常に
  `false`/`null`）。トップレベルの `readiness_checked` / `credentials_checked` /
  `provider_available` も同様に、この inspection が offline・静的であることを
  明示する（live probe の実装は out of scope）。
- `--provider auto`: `--profile` を省略すると `failure_class:
  "profile_required_for_auto"` で fail-closed する（`PROVIDER_AUTO_ELIGIBLE_PROFILES`
  がプロファイル単位のゲートであるため）。ineligible な profile を指定した
  場合は routing を読み込まず `profile_eligible: false` のみを返す（上記
  「制御フロー順序」参照）。eligible な profile の場合のみ
  `runtime_order`（`PROVIDER_AUTO_RUNTIME_ORDER` そのもの）、
  `profile_eligible: true`、`provider_candidates`（`runtime_order` の各
  provider ごとの解決結果。`gemini` 候補は `resolved_chain` を、`agy` 候補は
  agy 分岐と同じ capability 情報を持つ）、`consumer_constraints`
  （`fan_out: {supported: false, reason_code: "provider_auto_attempts_unbudgeted_v1"}`
  — `PROVIDER_AUTO_FAN_OUT_UNSUPPORTED_REASON_CODE`（`run_gemini_headless.py`
  で定義）をそのまま参照する。理由は「単に逐次実行だから」ではなく、
  `PROVIDER_AUTO_RETRYABLE_FAILURE_CLASSES` / `get_retry_budget()` が
  provider ごとの試行回数/backoff 予算を定義しており、2 provider を
  並行実行すると `attempts_by_model` / `provider_attempts` の監査可能性と
  retry_budget の境界が壊れるため。`agy_fallback_requires_prompt: true` —
  `_validate_agy_request()` が非空 `prompt` を必須とする、
  `explicit_model_survives_fallback: false` — `_validate_agy_request()` は
  `request["model"]` を拒否するため、gemini 側で指定した explicit model は
  agy へのフォールバックを生き残らない）を出力する。

## 関連ファイル

- `scripts/run_gemini_headless.py`: `DEFAULT_MODEL_ROUTING`, `load_model_routing`, `resolve_model_chain`, `run_delegation`
- `config/model_routing.yaml`: 任意オーバーライド設定
- `SKILL.md`: 利用者向け要点（progressive disclosure）
