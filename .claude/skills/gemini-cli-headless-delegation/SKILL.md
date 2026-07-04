---
name: gemini-cli-headless-delegation
description: Gemini CLI を wrapper 経由で非対話 delegation する shared skill。巨大ログ調査、構造化された技術調査、根拠付き比較を構造化 request 契約で委譲したいときに使う。
disable-model-invocation: true
---

# Gemini CLI Headless Delegation

## tool_profile 早見表

| tool_profile | 用途 |
|---|---|
| `no_tools` | ツール不使用・コンテキスト読み取り専用 |
| `grounded_research` | provider-aware: `provider=gemini`（既定）は Gemini API Google Search grounding、`provider=agy` は AGY native WebSearch/WebGrounding（`agy -p`、Gemini API 不使用、timeout_sec: 300+ 推奨）。両者は別実装であり、AGY 側は machine-verifiable tool-call トレース必須。詳細は `references/provider-mapping.md` / `references/usage-contract.md` 参照。 |
| `local_asset_research` | Serena MCP read-only によるローカル資産調査 |
| `proposal_only` | 実装案・Issue 本文案・patch proposal のドラフト生成 |
| `github_research` | GitHub read-only 調査（gh コマンド allowlist）|

詳細は `references/usage-contract.md`（SSOT）・`references/model-routing.md`・`references/result-surface.md` を参照。

## Workflow（作業手順）

0. **setup_check で依存ツール・trusted folder・Serena MCP・settings.json を確認する（必須）**:
   ```bash
   uv run --locked python3 .claude/skills/gemini-cli-headless-delegation/scripts/setup_check.py --json
   ```
   `ok: false` → `recovery` に従って対処。副作用ある操作（trustedFolders / .gemini/settings.json 変更）は `--fix` を付ける。

1. **request JSON を build_request.py で生成する（推奨）**:
   ```bash
   uv run --locked python3 .claude/skills/gemini-cli-headless-delegation/scripts/build_request.py \
     --profile github_research \
     --objective 'Issue #313 と PR #321 を gh issue view / gh pr view で調査する' \
     --context-file .claude/skills/gemini-cli-headless-delegation/references/usage-contract.md \
     --gh-issue 313 --gh-pr 321 \
     --output /tmp/gemini/request.json
   ```
   または手動で `delegation_request_v1` JSON を作成する（`references/usage-contract.md` 参照）。

2. **preflight を実行する（必要に応じて agy の grounded_research 検証を含める）**:
```bash
uv run --locked python3 .claude/skills/gemini-cli-headless-delegation/scripts/preflight_gemini_headless.py \
  --output-file tmp/gemini-headless-preflight.json
```
`agy` の grounded_research を含む検証が必要な場合:
```bash
uv run --locked python3 .claude/skills/gemini-cli-headless-delegation/scripts/preflight_agy.py \
  --grounded-research --json
```
   `ok: false` → `failure_reason` / `next_action` を確認して修正する。

3. **`scripts/run_gemini_headless.py` で request を検証・実行する**:
   ```bash
   uv run --locked python3 .claude/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py \
     --request-file /tmp/gemini/request.json --output-file /tmp/gemini/result.json
   ```
   validate-only（Gemini CLI 未実行）は `--validate-only` を指定する。結果は `result_surface.summary` / `.primary_artifact` / `.next_action` を優先参照する。

## Grounded Research Retry Policy（再試行運用ガバナンス）

このセクションは `tool_profile: grounded_research` における **wrapper-level retry の運用境界**を定義する governance 文書である。wrapper script (`scripts/run_gemini_headless.py`) の実 retry 挙動・retry 回数・`delegation_result_v1` の attempt フィールド仕様は `references/usage-contract.md` および `references/model-routing.md` を SSOT とし、本セクションはそれらと衝突しない範囲の運用方針のみを定める。

- 対象（運用境界として retry が妥当な失敗）:
  - `auth_error`
  - transient CLI failure
- 運用境界:
  - 同一 request の retry は wrapper 内に閉じ、caller / orchestrator 側で重ねて retry しない
  - retry 中に `tool_profile` / request body を変更しない
  - credential mutation なし、再ログインなし
- 再試行後も失敗した場合:
  - wrapper は失敗を隠さず返す
  - caller は `failure_class`（および wrapper が提供する attempt 情報があれば）を読んで fallback / escalation を判断する

この policy は wrapper-level retry の governance のみを扱う。grounding quality の判定、critical claim ごとの direct fallback、`WEB_RESEARCH_RESULT_V1` の最終分類は `web-researcher` の責務とする。wrapper script の挙動変更が必要になった場合は、本ファイルではなく `references/usage-contract.md` / `scripts/run_gemini_headless.py` / tests を更新する別 Issue を起票する。


## Gemini OAuth 終了・API key 暫定運用・agy 移行

Google ログイン経由の Gemini CLI 認証が終了した場合、`setup_check.py` は `auth.status: oauth_sunset`
または `auth.status: ineligible_tier` を返す。

### 暫定回避: GEMINI_API_KEY

API key が利用可能な場合は `GEMINI_API_KEY` 環境変数に設定することで暫定的に Gemini 経路を継続できる。

- **API key は暫定回避であり、恒久対応ではない。**
- key の値は stdout / stderr / JSON 出力に絶対に含めない（`setup_check.py` が existence のみ検出する）。
- key を `.env` / コードベース / PR 本文に commit しない。

```bash
# 暫定運用例（セッション内のみ有効）
export GEMINI_API_KEY=<your-key>
uv run --locked python3 .claude/skills/gemini-cli-headless-delegation/scripts/setup_check.py --json
# → auth.status: "authenticated_api_key"
```

### 恒久対応: agy (Antigravity CLI) への移行

**恒久対応は Antigravity CLI (agy) への移行**である。parent Issue #104 を参照。

- API key 暫定運用は #104 が完了するまでのブリッジであり、agy 移行が完了したら不要になる。
- agy 移行の進捗は `docs/dev/current-focus.md` および #104 を参照。

## AGY 認証診断・既知の環境課題（WSL2 / non-TTY）（Issue #1267）

`preflight_agy.py` の `run_preflight()` は、すべての `agy_preflight_result/v1`
（success / CLI missing / smoke failure / timeout / grounded・local-asset sub-check
failure のいずれでも）に `auth: agy_auth_diagnostics_v1` を含める。
`setup_check.py --provider agy --json` はこの `auth` オブジェクトを
`agy_preflight.auth` にそのまま surfacing する（schema drift なし。SSOT は
`preflight_agy.py`）。

`agy_auth_diagnostics_v1` は以下を含む:

- `auth_mode`（`unknown` / `system_keyring_cached` / `google_sign_in_required` /
  `api_key_env_present` / `unauthenticated` / `auth_probe_failed`）— 推定された認証状態と、
  `auth_mode_confidence`（`observed` / `inferred` / `unknown`）— その確信度
- `keyring`（`available` / `backend_hint` / `failure_class`）— system keyring への到達可否
- `tty`（`stdin_isatty` / `stdout_isatty` / `stderr_isatty` / `noninteractive_mode`）— 端末接続状態
- `platform`（`os` / `is_wsl` / `wsl_hint`）— 実行環境（WSL 判定を含む）
- `recovery_action` — 人間が次に取るべき復旧手順の説明文

診断用の env snapshot（`DBUS_SESSION_BUS_ADDRESS_present` 等の boolean のみ）と、
agy subprocess 実行用の minimal env（`_minimal_agy_env()`）は分離されている。
診断結果に環境変数の値そのものが含まれることはない。

### 既知の問題 1: WSL2 での keyring 未到達 / OAuth 再認証

WSL2 環境ではデフォルトで D-Bus session bus が起動していないため、
`DBUS_SESSION_BUS_ADDRESS` が未設定なことが多く、secret-service 経由の
system keyring バックエンドに到達できない（`auth.keyring.failure_class:
system_keyring_unavailable`、`auth.platform.is_wsl: true`）。この状態では agy が
キャッシュ済み認証情報を読めず、再認証（Google Sign-In）が要求されることがある。

recovery action（推奨される復旧手順）:

headless Linux（WSL2 含む）での keyring 運用は、D-Bus session を起動するだけでは
不十分なことが多い。GNOME Keyring daemon 自体の起動・unlock・**同一 D-Bus
session 内での実行**が必要になる:

```bash
# 1. D-Bus session に入る（起動のみで抜けてしまう dbus-launch --exit-with-session
#    より、以降のコマンドを同一 session 内で実行できる dbus-run-session を推奨する）
dbus-run-session -- sh -c '
  # 2. GNOME Keyring daemon を起動し、secret-service backend を unlock する
  eval "$(gnome-keyring-daemon --start --components=secrets)"
  export GNOME_KEYRING_CONTROL

  # 3. 同じ shell/session 内で preflight を実行する
  uv run --locked python3 .claude/skills/gemini-cli-headless-delegation/scripts/preflight_agy.py --json
'
```

`dbus-launch --exit-with-session` は D-Bus session bus を一時的に起動するだけで
GNOME Keyring daemon の起動・unlock は行わないため、上記の
`dbus-run-session` + `gnome-keyring-daemon --start` の代替（簡易的な bus 起動確認
用途）としてのみ位置づける。daemon の unlock を伴わない場合、`auth.keyring.available`
は `null`（`secret_service_dbus_session_present` — Issue #1267 fix_delta Blocker
2）のまま変わらないことに注意する。

D-Bus session / keyring daemon を用意できない場合は、対話的な TTY セッションで一度
`agy` の認証（Google Sign-In）を完了させてから、non-TTY で `agy -p` を実行する。

### 既知の問題 2: `agy -p` の non-TTY 実行時 silent stdout drop

`agy -p` を non-TTY（`stdin_isatty`/`stdout_isatty` が false）で実行すると、
認証が必要な状態でも exit code 0 かつ空 stdout を返すことがある
（`smoke.failure_class: agy_empty_stdout` / CI では `agy_output_missing`）。
これは "silent" な失敗であり、stderr/stdout に明示的な auth/keyring 証跡が
ない限り auth failure として再分類されない（`agy_empty_stdout` /
`agy_output_missing` は output-surface failure のまま維持される — Issue #1267
Required Result Contract）。

recovery action: 対話的 TTY セッションで `agy` の認証状態を確認・再ログインし、
その後 non-TTY 実行を再試行する。stderr に `keyring` / `sign in` /
`interactive login required` 等の文言が含まれる場合は、
`auth.auth_mode` / `auth.recovery_action` を優先的に参照する。

## Core Rules（基本ルール）

### Delegation Boundary
- Gemini 側の `file write` / `shell edit` / GitHub mutation は禁止。
- `post_to_issue_url` は wrapper 側の GitHub コメント投稿操作。`no_tools` / `grounded_research` のみ許可。
- 最終 file edit / shell 実行 / GitHub mutation は caller / orchestrator が保持する。

### Request Validation
- `tool_profile` は必ず明示し、暗黙既定を置かない。
- `context_files` が欠ける、参照先が見つからない、`objective` が曖昧すぎる、`instructions` が 2 件未満、`output_sections` が空なら fail-closed にする。
- `GEMINI.md` など ambient context を避けるため、wrapper は isolated temp cwd で Gemini を呼ぶ。

### Model Routing
- モデル選択・降格チェーン・quota retry・role 別優先列の詳細は `references/model-routing.md` を参照。
- 明示 `model` 指定時は降格せずその model のみで試行する。

## References
- `references/index.md`（normative rule の owner file 一覧）
- `references/usage-contract.md`（リクエスト JSON フィールド仕様・profile ルールの SSOT）
- `references/model-routing.md`（model routing スキーマ・role テーブル）
- `references/result-surface.md`、`references/runtime-portability.md`
- `references/isolated-cwd-rationale.md`、`references/delegation-task-classes.md`

## 出力制約 (OUTPUT_BUDGET_V1)

`docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` の制約に従う。
