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

```
wrapper 内降格（model_chain 試行） → 全 model 失敗 → chain_exhausted を返す
                                           ↓
                              caller-side fallback（ClaudeCode 直接生成）
```

wrapper が `ok: false` + `reason_code: "model_chain_exhausted"` を返した後に
caller（`web-researcher` 等）が ClaudeCode 直接生成 fallback を発動する。

## model-policy subcommand（`build_request.py model-policy`、Issue #1269）

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

### 出力スキーマ `delegation_model_policy/v1`

| フィールド | 説明 |
|---|---|
| `schema` | 常に `"delegation_model_policy/v1"` |
| `provider` / `role` / `profile` | CLI 引数のエコー |
| `ok` | `true`/`false`（fail-closed 時は `false`） |
| `failure_class` / `failure_reason` | fail-closed 時のみ非 null |
| `resolved_chain` | `provider=gemini` のときのみ非 null。`resolve_model_chain()` の戻り値そのもの |
| `actual_model` | 常に `null`（dry-run のため観測値を持たない） |

### AGY / auto の capability 表示ルール（Blocker 4 / Blocker 6）

- `--provider agy`: `run_delegation()` は `provider="agy"` に対して
  `resolve_model_chain()` を一切呼び出さない（AGY に model chain の概念が
  存在しない）ため、`resolved_chain` は常に `null`。実行時に返る
  `actual_model: "agy-default"`（リテラル固定値）は、model-policy の
  `actual_model` フィールドには**絶対に出力せず**、`legacy_compatibility_label`
  としてのみ表示する。`wrapper_capability`（`explicit_model_selection`:
  false、`role_based_model_chain`: false）と `upstream_capability`
  （`probed`: false、Antigravity CLI 自体は起動しない旨の note）を分離して
  出力する。
- `--provider auto`: `--profile` を省略すると `failure_class:
  "profile_required_for_auto"` で fail-closed する（`PROVIDER_AUTO_ELIGIBLE_PROFILES`
  がプロファイル単位のゲートであるため）。`--profile` 指定時は
  `runtime_order`（`PROVIDER_AUTO_RUNTIME_ORDER` そのもの）、
  `profile_eligible`（指定 profile が `PROVIDER_AUTO_ELIGIBLE_PROFILES` に
  含まれるか）、`provider_candidates`（`runtime_order` の各 provider ごとの
  解決結果。`gemini` 候補は `resolved_chain` を、`agy` 候補は agy 分岐と同じ
  capability 情報を持つ）、`consumer_constraints`
  （`fan_out: false` — `provider_auto_dispatch()` は
  `PROVIDER_AUTO_RUNTIME_ORDER` を逐次試行し並行実行しない、
  `agy_fallback_requires_prompt: true` — `_validate_agy_request()` が
  非空 `prompt` を必須とする、`explicit_model_survives_fallback: false` —
  `_validate_agy_request()` は `request["model"]` を拒否するため、gemini
  側で指定した explicit model は agy へのフォールバックを生き残らない）
  を出力する。

## 関連ファイル

- `scripts/run_gemini_headless.py`: `DEFAULT_MODEL_ROUTING`, `load_model_routing`, `resolve_model_chain`, `run_delegation`
- `config/model_routing.yaml`: 任意オーバーライド設定
- `SKILL.md`: 利用者向け要点（progressive disclosure）
