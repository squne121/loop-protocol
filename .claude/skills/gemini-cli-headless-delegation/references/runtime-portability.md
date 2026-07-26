# Runtime Portability / 実行ポータビリティ

## Supported Environments / 対応環境

| 環境 | 状態 | 備考 |
|------|------|------|
| Claude Code on WSL2 (Ubuntu 24.04) | primary | 主要サポート対象 |
| Codex CLI | Followup Issue 対応 | 未検証・Out of Scope |

## Prerequisites / 事前条件

### `provider=gemini`

- Node.js 18+ がインストール済みであること（`node --version` で確認）
  - `gemini` CLI は `@google/gemini-cli` Node.js パッケージとして提供される
  - nvm 経由のインストールを推奨: `nvm install --lts`
- `gemini` CLI がインストール済みであること（`gemini --version` で確認）
  - インストール方法: `npm install -g @google/gemini-cli`
- Python 3.10+ が利用可能であること
- `uv` が利用可能であること（テスト実行用）
- Google アカウントによる認証が完了していること

### `provider=agy`（共通前提 / 全 tool_profile 共通）

- `agy` CLI がインストール済みであること（`agy --version` で確認）
- Python 3.10+ が利用可能であること
- `uv` が利用可能であること（`setup_check.py` / `preflight_agy.py` 実行用）
- Node.js / `gemini` CLI / trustedFolders / Gemini OAuth は不要（`agy` (Antigravity CLI) は Google OAuth 経由の Gemini CLI 認証を使わない）
- `setup_check.py --provider agy --json` は check-only で、`.gemini/` や trustedFolders を変更しない

> **注意**: 上記は `no_tools` / `proposal_only` / `grounded_research` の共通前提であり、
> `tool_profile=local_asset_research` を使う場合は下記「`provider=agy` + `local_asset_research` の wrapper-side Serena 前提」が別途必要になる。
> `uvx` / Serena MCP を「不要」とするのは誤りであり、`local_asset_research` では wrapper 側が必須で使用する。

### `provider=agy` + `local_asset_research`（wrapper-side Serena 前提）

`tool_profile=local_asset_research` を使う場合のみ、上記共通前提に加えて以下が必要になる。
**agy 自身が Serena MCP を呼び出すわけではない。** wrapper（`run_gemini_headless.py`）側が pinned Serena MCP server を
`subprocess.Popen(command, cwd=repo_root, stdin=PIPE, stdout=PIPE, stderr=PIPE, text=True, shell=False, env=_minimal_agy_env(), bufsize=1)`
で起動し、`initialize` → `notifications/initialized` → `tools/list` → read-only `tools/call` の順に MCP JSON-RPC を実行して
repo-relative な read-only evidence envelope を構築し、それだけを agy への prompt に含める。

- `uvx` が利用可能であること（pinned Serena MCP server の起動に使う。`.agents/mcp_config.json` の `command`/`args` を正本とする）
- `.agents/mcp_config.json` が存在し、`mcpServers.serena` に pinned Serena ref（`git+https://github.com/oraios/serena@<pinned_ref>`）、`trust: false`、`includeTools`（read-only allowlist）、`excludeTools`（dangerous denylist）が設定されていること
- `references/serena-tool-manifest.json`（`serena_tool_manifest_v1`）の `pinned_ref` / `read_only_allowlist` / `dangerous_denylist` / `known_tools` と、実際に起動した Serena MCP の `tools/list` 応答が一致していること。drift（`known_tools` に存在しない tool が返る、または manifest 記載の tool が消えている）は fail-closed する
- 互換用に `.gemini/settings.json` にも同じ Serena 設定（`mcp.allowed == ["serena"]` と `mcpServers.serena`）を用意すること。`preflight_gemini_headless.py` / `run_gemini_headless.py._validate_local_asset_research_settings` は `.gemini/settings.json` と `.agents/mcp_config.json` の両方を検証する
- Gemini OAuth / trustedFolders は Serena MCP 起動そのものには不要（Serena は wrapper が直接起動する子プロセスであり、Gemini CLI 経由ではない）

### delegation_audit_v1 公開安全フィールド（監査ログ）

- `local_asset_metadata`（`tool_profile=local_asset_research` 時）:
  - `retrieval_status`: `succeeded` / `failed` / `not_applicable`
  - `retrieval_mode`: `live_serena_mcp`（実行時）
  - `serena_manifest_id`: `serena_tool_manifest_v1:<pinned_ref>`
  - `serena_pinned_ref`: manifest の pinned ref
  - `read_only_allowlist_sha256`: `read_only_allowlist` の JSON SHA-256
  - `dangerous_denylist_sha256`: `dangerous_denylist` の JSON SHA-256
  - `live_tools_list_sha256`: live `tools/list` 応答 tool 名配列の JSON SHA-256
  - `manifest_drift_failed`: manifest 整合性逸脱検知
  - `context_files_count`: 対象 context ファイル件数
  - `evidence_record_count`: Serena 証跡レコード件数
  - `failure_class`: 取得系失敗分類

- `auth_diagnostics_metadata`（AGY 認証関連失敗）:
  - `schema`: `agy_auth_diagnostics_v1`
  - `auth_failure_class`: `agy_auth_required` / `agy_permission_denied`
  - `auth_mode`, `keyring_available`, `tty_mode`, `dbus_session_bus_present`, `xdg_runtime_dir_present`, `ssh_session_detected`, `recovery_action`


## Claude Code (WSL2) での実行手順 / Execution from Claude Code

### 1. Preflight で環境確認

```bash
uv run python3 .claude/skills/gemini-cli-headless-delegation/scripts/preflight_gemini_headless.py \
  --output-file tmp/gemini-headless-preflight.json
cat tmp/gemini-headless-preflight.json
```

### 2. Request JSON を作成

`provider=gemini` では `delegation_request_v1` スキーマで request ファイルを作成する。
詳細な必須項目と request contract の境界は `references/usage-contract.md` を参照する。

```json
{
  "schema": "delegation_request_v1",
  "objective": "...",
  "instructions": ["...", "..."],
  "tool_profile": "no_tools",
  "output_sections": ["Summary", "Findings"],
  "context_files": ["path/to/context.md"]
}
```

### 3. Wrapper 実行手順

```bash
uv run python3 .claude/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py \
  --request-file <request.json> \
  --output-file <result.json>
```

### 4. 結果確認

```bash
cat <result.json>
# ok, response_text, actual_model, warnings, stderr を確認する
```

## Serena MCP セットアップ

`local_asset_research` プロファイルを使用するには Serena MCP が起動可能である必要がある。
インストールは `uvx` 経由で行い、明示的な `pip install` は不要。
正本は `.agents/mcp_config.json` の `mcpServers.serena` であり、pinned ref・`includeTools`・`excludeTools` を
`references/serena-tool-manifest.json`（`serena_tool_manifest_v1`）と一致させる必要がある。

```bash
# 起動可能性を確認（pinned ref を明示。--help で終了するため実際のインストールは行われない）
uvx --from git+https://github.com/oraios/serena@<pinned_ref> serena start-mcp-server --project-from-cwd --help
```

### `.agents/mcp_config.json`（正本 / AGY 用 MCP 設定）

`.agents/mcp_config.json` は AGY provider が参照する MCP サーバー設定の正本であり、`local_asset_research` の
wrapper-side Serena 起動はこのファイルの `mcpServers.serena` を読む（`run_gemini_headless.py._load_serena_from_mcp_config`）。

```json
{
  "mcpServers": {
    "serena": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/oraios/serena@<pinned_ref>",
        "serena",
        "start-mcp-server",
        "--project-from-cwd"
      ],
      "trust": false,
      "includeTools": [
        "find_file",
        "find_referencing_symbols",
        "find_symbol",
        "get_symbols_overview",
        "list_dir",
        "search_for_pattern"
      ],
      "excludeTools": [
        "activate_project", "create_text_file", "delete_memory", "edit_memory",
        "execute_shell_command", "find_declaration", "find_implementations",
        "get_current_config", "get_diagnostics_for_file", "initial_instructions",
        "insert_after_symbol", "insert_before_symbol", "list_memories", "onboarding",
        "read_file", "read_memory", "rename_memory", "rename_symbol",
        "replace_content", "replace_in_files", "replace_symbol_body",
        "safe_delete_symbol", "write_memory"
      ]
    }
  }
}
```

`<pinned_ref>` は `references/serena-tool-manifest.json` の `pinned_ref` フィールドの値を使う（`git ls-remote` で取得した commit SHA を pin する）。
`command`/`args` は `uvx ... serena start-mcp-server --project-from-cwd` の形（サブコマンドをハイフンで連結した単一トークン名の旧テンプレは現行 contract ではない）。
`includeTools` は read-only allowlist（manifest の `read_only_allowlist` と一致）、`excludeTools` は dangerous denylist（manifest の `dangerous_denylist` と一致）を必ず含める。
`trust` は必ず `false` にする。

### `.gemini/settings.json`（互換用）

`.gemini/settings.json` は Gemini CLI 側の互換設定として引き続き必要であり、`.agents/mcp_config.json` と同じ
`mcpServers.serena` 設定（pinned ref・`includeTools`・`excludeTools`）を持たせる。
`preflight_gemini_headless.py` / `run_gemini_headless.py._validate_local_asset_research_settings` は
`.gemini/settings.json` と `.agents/mcp_config.json` の **両方** を、`references/serena-tool-manifest.json` に対して
machine-checkable に検証する（`mcp.allowed == ["serena"]`、pinned ref 一致、`includeTools` が read-only allowlist と完全一致、
`excludeTools` が dangerous denylist を含む、のいずれかに違反すると fail-closed）。

> **既知の未解消差分**: `setup_check.py --fix` が `.gemini/settings.json` 不在時に自動生成するテンプレ（`_SETTINGS_TEMPLATE`）は
> 本書執筆時点で unpinned（commit ref を含まない source 指定）かつ `excludeTools` を含まない旧形式のままであり、
> 上記の pinned manifest 検証をそのままでは満たさない。この自動生成テンプレの更新は本 Issue の Allowed Paths（`scripts/setup_check.py` は対象外）を
> 超えるため、本書では「実際に repo に存在する `.gemini/settings.json` / `.agents/mcp_config.json` が満たすべき正しい形」を記述するに留め、
> `setup_check.py --fix` のテンプレ自体の追随は別 Issue で扱う。

## Trusted Folder の programmatic 登録手順

`~/.gemini/trustedFolders.json` へのリポジトリパス登録は `setup_check.py` が自動的に行う。

- `setup_check.py` は `git rev-parse --show-toplevel` でリポジトリルート絶対パスを取得する。
- 既にそのパス（または親ディレクトリ）が登録済みであれば no-op（idempotent）。
- ファイルが存在しない場合は新規作成する。

```bash
# setup_check.py を実行することで trusted folder も登録される
uv run python3 .claude/skills/gemini-cli-headless-delegation/scripts/setup_check.py --json
```

手動で登録する場合は以下を実行:

```bash
REPO_ROOT=$(git rev-parse --show-toplevel)
python3 -c "
import json, pathlib
p = pathlib.Path.home() / '.gemini' / 'trustedFolders.json'
entries = json.loads(p.read_text()) if p.exists() else []
if '$REPO_ROOT' not in entries:
    entries.append('$REPO_ROOT')
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(entries, indent=2) + '\n')
    print('added')
else:
    print('already trusted')
"
```

## アカウント認証について（人間責任の事前準備）

Gemini CLI の Google OAuth 認証は **人間が事前に完了させる必要がある**。
`setup_check.py` はキャッシュ済み認証情報の有効性を smoke prompt で確認するが、
interactive login の自動化は行わない（Out of Scope）。

```bash
# 認証が必要な場合（人間が手動実行）
gemini auth login

# 認証状態確認
gemini --prompt "ok" --model gemini-2.0-flash
```

認証が完了していない状態で `local_asset_research` を実行すると `preflight_gemini_headless.py` が
`failure_class: "trusted_workspace_required"` または OAuth エラーで fail-closed になる。

## uv 優先方針

このスキルのすべての Python スクリプト実行は `uv run python3 ...` を使用することを推奨する。
これにより依存ライブラリのバージョン整合性が保たれる。

```bash
# テスト実行（uv 優先 — pyyaml 等の依存を明示的に指定）
uv run --with pytest --with pyyaml python -m pytest tests/

# setup_check 実行
uv run python3 .claude/skills/gemini-cli-headless-delegation/scripts/setup_check.py --json

# preflight 実行
uv run python3 .claude/skills/gemini-cli-headless-delegation/scripts/preflight_gemini_headless.py \
  --output-file tmp/gemini-headless-preflight.json
```

`python3 ...` の直接実行は環境によって依存ライブラリが不足している場合があるため、
`uv run` 経由を標準とする。


## Gemini OAuth 終了後の運用境界

Google OAuth 経由の Gemini CLI 認証が終了した場合、以下の運用境界に従う。

### API key 暫定回避（一時的）

`GEMINI_API_KEY` 環境変数を設定することで Gemini 経路を継続できる。
**API key は暫定回避であり、恒久対応ではない。**

| 項目 | 境界 |
|------|------|
| 利用目的 | agy 移行完了までのブリッジ |
| key の有効期限 | 無期限ではないため定期的に確認する |
| key の保存 | セッション内環境変数のみ。コードベース / `.env` / PR 本文への commit 禁止 |
| key の出力 | 値を stdout / stderr / JSON に絶対に含めない（existence のみ検出） |

```bash
# 暫定運用（セッション内のみ）
export GEMINI_API_KEY=<your-key>
uv run python3 .claude/skills/gemini-cli-headless-delegation/scripts/setup_check.py --json
# auth.status が "authenticated_api_key" になれば暫定運用中
```

### 恒久対応: agy (Antigravity CLI) 移行

**恒久対応は parent Issue #1265 の agy 移行である。**
`#104` は本移行の恒久正本ではない。API key 暫定運用は #1265 配下の child issue 群による agy provider 実装が完了したら不要になる。

- agy 移行の進捗は #1265 と `.claude/skills/gemini-cli-headless-delegation/references/` 配下の current references（本ファイル、`provider-mapping.md`、`usage-contract.md`）を参照する。
- agy が利用可能になったら `gemini-cli-headless-delegation` skill の provider を切り替える。

---

## agy Provider: 実行ポータビリティ

### AC4: agy -p 手動 smoke 手順 / Manual Smoke

`agy -p`（`--print` / `--prompt` フラグ）を使った手動動作確認の手順を示す。

> 実機証跡生成および証跡の保存は #109 / #110 または別 Issue の責務。
> 本手順は docs-only（手順文書化のみ）であり、自動実行や証跡保存は対象外。

```bash
# 1. agy が利用可能か確認
${AGY_BIN:-agy} --version

# 2. isolated temp cwd を作成
SMOKE_TMPDIR=$(mktemp -d)

# 3. agy -p で sentinel 完全一致を確認する
EXPECTED="LOOP_AGY_SMOKE_OK"
ERR_FILE="$(mktemp)"
OUTPUT="$(cd "${SMOKE_TMPDIR}" && "${AGY_BIN:-agy}" -p "Return exactly: ${EXPECTED}" 2>"${ERR_FILE}")"
EXIT_CODE=$?

# 4. 終了コードと stdout を確認（sentinel exact match）
if [ "${EXIT_CODE}" -ne 0 ]; then
  echo "FAIL: agy exited with ${EXIT_CODE}"
elif [ "${OUTPUT}" != "${EXPECTED}" ]; then
  echo "FAIL: agy_output_mismatch (got: ${OUTPUT})"
else
  echo "OK: agy responded with expected sentinel"
fi
```

### `setup_check.py --provider agy` の expected shape / 期待 JSON 形状

`setup_check.py --provider agy --json` は次を返す。

- `provider: "agy"`: 利用した provider 名を返す
- `selected_provider: "agy"`: auto 選択時でも最終選択を明示する
- `tools`: `agy` / `python3` / `uv` の version probe 結果を返す
- `agy_preflight`: `preflight_agy.py --json` 相当の sanitized result を埋め込む
- `skipped_gemini_checks`: `trusted_folders` / `serena_mcp` / `gemini_settings` / `auth` / `node` / `gemini` / `uvx` を明示する

`setup_check.py --provider agy --json --fix` は mutation を行わず、`unsupported_provider_option` で fail-closed する。
`setup_check.py --provider auto --json` は `agy` を先に probe し、成功時は `selected_provider: "agy"` を返す。
`setup_check.py --provider auto --json --fix` は副作用対象が曖昧なため、明示的に `unsupported_provider_option` で拒否する。

### AC5 / AC10: AGY_BIN の上書き優先順位と path 取扱い

`${AGY_BIN:-agy}` 形式により、agy バイナリのパスをオーバーライドできる。

```bash
# AGY_BIN が未設定の場合: PATH 上の agy を使用
${AGY_BIN:-agy} -p "test prompt"

# AGY_BIN が設定されている場合: その値を使用
export AGY_BIN=/usr/local/bin/agy
${AGY_BIN:-agy} -p "test prompt"

# カスタムバイナリパスを指定する場合
export AGY_BIN=/opt/agy/bin/agy
${AGY_BIN:-agy} --version
```

#### AGY_BIN precedence ルール

| 状態 | 使用するバイナリ |
|------|------|
| `AGY_BIN` が設定されている | `$AGY_BIN` を使用 |
| `AGY_BIN` が未設定 | PATH 上の `agy` を使用 |

#### raw_command 表示時の情報漏洩回避方針

`AGY_BIN` の値をログ・stdout・エラーメッセージに出力する場合は、以下の情報漏洩防止方針に従う。

- prompt 本文は `raw_command` に含めない（必要な場合は length または hash のみ記録する）
- `AGY_BIN` は basename または `<AGY_BIN>` placeholder で表示する（絶対パスをそのまま出力しない）
- `$HOME` 配下の絶対パスは `$HOME/...` 形式に再マスクする（展開後の絶対パスをそのまま記録しない）
- secret らしい値・token・query string・認証情報を含む path は出力禁止
- `resolved_path` は basename または `$HOME/...` mask だけを保存し、フル絶対パスは evidence に残さない

### AC9: non-TTY / pipe / CI 環境での fail-closed

`agy -p` を non-TTY 環境（pipe / CI / headless 実行）で呼び出した場合、以下の挙動に注意する。

| 状態 | 判定 | エラーコード |
|------|------|------|
| exit 0 かつ sentinel 完全一致 | ok（smoke pass） | - |
| exit 0 かつ sentinel 不一致（stdout に出力あり） | fail-closed | `agy_output_mismatch` |
| exit 0 かつ stdout が空 | fail-closed | `agy_empty_stdout` |
| exit 0 かつ stdout が空（CI 環境） | fail-closed | `agy_output_missing` |
| exit non-0 | fail-closed | exit code に応じた分類 |

non-TTY / pipe 環境で `agy -p` が exit 0 かつ stdout 空になった場合は、
agy が TTY 検出により出力を抑制した可能性があるため、**fail-closed** として扱う。
stdout が空の場合や sentinel 不一致の場合に PASS として扱う設計は禁止（partial / silent response を PASS に変換しない）。

## 隔離済み一時作業ディレクトリ・最小環境変数・shell=False の制約

`run_gemini_headless.py` の `_run_agy()` は agy 呼び出しのたびに `tempfile.TemporaryDirectory()` で
**隔離された一時作業ディレクトリ（isolated temp cwd）** を生成し、その作業ディレクトリから
`subprocess.run(..., shell=False)` で agy を起動する。
`shell=False` を指定することでシェル経由のコマンド注入（shell injection）の余地を排除し、
リポジトリのルートディレクトリを起動時の cwd として渡さない安全側の設計を採る。

## minimal env と認証境界 / 認証依存の注意

`preflight_agy.py` と `run_gemini_headless.py` の `provider=agy` 経路は secret leakage を避けるため、
child process に親 env をそのまま継承せず、`PATH` / `HOME` / locale / XDG 系だけを allowlist する安全側ポリシーを採る。

- `GEMINI_API_KEY` / `AGY_API_KEY` のような secret env は継承しない
- 認証が system keyring / desktop session / dbus / runtime dir に依存する環境では fail-closed し得る
- その場合は allowlist 拡張の可否を人間レビューで判断する
- stdout / stderr sample は redact-before-truncate の順序で保存する

### `materialize_isolated_agy_workspace()` の認証 surface 最小化と read-only 境界（Issue #1779）

#1726 → #1730 → #1740 → #1743 の一連の実装は、「認証が通らない → 仮説 → 追加」の反復で
5 つの認証 surface（`DBUS_SESSION_BUS_ADDRESS` / `XDG_RUNTIME_DIR` / `GOOGLE_APPLICATION_CREDENTIALS` /
`gcloud_adc_path` / `agy_oauth_token_path`）を無条件・デフォルトで露出する設計になっていた。
#1494 に対する敵対的再監査（controlled ablation experiment）の結果、以下が実証された。

```yaml
AGY_AUTH_ABLATION_V1:
  decision:
    oauth_only_sufficient: true
    dbus_required: false
    gcloud_adc_required: false
```

また read-only 境界についても、`_expose_agy_oauth_token_read_only()` は `Path.symlink_to()` のみで
OS レベルの強制を伴わず、symlink 経由の書込みが実際に成功することが実証された。

```yaml
AGY_READONLY_BOUNDARY_V1:
  symlink_write_test:
    dummy_file_overwritable_via_symlink: true
  bwrap_available: true
  ro_bind_poc_result: success
  decision:
    readonly_technically_enforced: false
    recommended_terminology: degraded_symlink_reachability
    recommended_mechanism: ro_bind
```

#### `auth_profile`: 認証 surface 最小化（`tool_profile` とは別軸）

`materialize_isolated_agy_workspace(profile, *, auth_profile=AGY_AUTH_PROFILE_MINIMAL)` の
`auth_profile` 引数（既定値 `AGY_AUTH_PROFILE_MINIMAL`）は、`no_tools` / `local_asset_research` /
`grounded_research` / `proposal_only` の `tool_profile`（AGY *tool 呼び出し* を制御する軸）とは
**別軸** のパラメータであり、AGY *認証 surface の到達可能性* のみを制御する。

- `AGY_AUTH_PROFILE_MINIMAL`（既定）: `agy_oauth_token_path` のみを露出する。
  `DBUS_SESSION_BUS_ADDRESS` / `XDG_RUNTIME_DIR` / `GOOGLE_APPLICATION_CREDENTIALS` は env に含まれず、
  `gcloud_adc_path` は `None` になる。`AGY_AUTH_ABLATION_V1` により認証成功に必要十分であることが
  実証済みの surface のみを既定で露出する。
- `AGY_AUTH_PROFILE_EXTENDED`: #1726 / #1730 が追加した 4 surface（DBus / XDG_RUNTIME_DIR /
  GOOGLE_APPLICATION_CREDENTIALS / gcloud ADC）を明示的な opt-in として従来どおり露出する。
  将来これらが必要になる環境を想定し、実装（`_expose_gcloud_adc_read_only()` 等）自体は削除せず維持する。

`run_gemini_headless.py::_run_agy()` / `build_agy_run_context()` は、`auth_profile` を明示指定しない
限り `AGY_AUTH_PROFILE_MINIMAL` で動作する（既存呼び出し元の挙動を破壊的に変更しない形での既定値変更）。

#### `agy_oauth_token_readonly_mode`: read-only 境界の真正性

`IsolatedAgyWorkspace.agy_oauth_token_readonly_mode` は、`agy_oauth_token_path` の read-only 主張が
実際にどう担保されているかを明示する（値: `kernel_enforced_ro_bind` | `degraded_symlink_reachability` | `absent`）。

- `kernel_enforced_ro_bind`: `bwrap` が利用可能な環境では、`_build_bwrap_ro_bind_prefix()` が
  `bwrap --dev-bind / / --tmpfs <dir> --ro-bind <real> <link> -- <command...>` 形の argv prefix を
  組み立てる。`run_gemini_headless.py::_run_agy()` は、実際に `agy` subprocess を起動する
  `subprocess.run(command, ...)` 呼び出し箇所のみでこの prefix を `command` へ前置する。これにより
  トークンファイルへの書込み試行はカーネルレベルで `EROFS`（Read-only file system）となり実際に失敗する
  （読み取りは成功する）ことを `test_agy_permission_policy_readonly_boundary.py` の hermetic 統合テスト
  （ダミー fixture、実 credential は使わない）で検証している。
- `degraded_symlink_reachability`: `bwrap` が利用不可能な環境でのフォールバック。`agy_oauth_token_path`
  は引き続き到達可能（symlink）だが、OS レベルの read-only 強制は行われない。関数名・戻り値・ログ文字列
  から「read_only」という未実証の主張は除去し、この用語を明示的に使う。
- `absent`: 実ホストに `$HOME/.gemini/antigravity-cli/antigravity-oauth-token` が存在しない場合。

`no_tools` / `local_asset_research` プロファイルでは、`bwrap` が利用不可能かつ実トークンファイルが
存在する場合に `AgyReadOnlyBoundaryError` で fail-closed する（workspace を作らない）。
`grounded_research` / `proposal_only` は `degraded_symlink_reachability` での続行を許容する。
`bwrap` が CI 実行環境に存在しない場合でも、実トークンファイルが存在しない（CI の通常状態）限り
fail-closed は発生せず、CI green は `bwrap` の存在に依存しない。

#### `_WORKSPACE_DENY_GATE_HOOK_SOURCE`: no-op placeholder の明示

AGY 公式ドキュメント（`https://antigravity.google/docs/cli/reference` / `https://antigravity.google/docs/cli/using`、
#1758 が `toolPermission` 設定を確認したのと同じソース）を #1779 で再調査したが、実際に機能する
`PreToolCall`/`PreToolUse` 相当のフック機構は確認できなかった。`_WORKSPACE_DENY_GATE_HOOK_SOURCE` は
モジュール冒頭コメントで no-op placeholder であることを明示するファイルとして生成され続ける。
tool deny の唯一の実効的な防御機構は、`PROFILE_ALLOWED_TOOLS` ベースの静的 allowlist
（`resolve_tool_permission()` / `build_workspace_permission_policy()` の `permissions.allow`/`.deny`）である。

### `materialize_isolated_agy_workspace()` の環境変数 allowlist 拡張（Issue #1726、superseded_by: #1779, 2026-07-26）

> **注意（#1779 による再検証日時: 2026-07-26）**: 以下は #1726 起票時点の記録として維持するが、
> `DBUS_SESSION_BUS_ADDRESS` / `XDG_RUNTIME_DIR` は #1779 の `AGY_AUTH_PROFILE_MINIMAL`（既定）では
> もはや既定で露出されない。`AGY_AUTH_PROFILE_EXTENDED` を明示指定した場合のみ、以下の記述どおりに
> 動作する。実装自体は削除していない（将来必要になる環境のための opt-in として維持）。

`agy_permission_policy.py` の `materialize_isolated_agy_workspace()` は、`PATH` / `LANG` / `LC_ALL` / `TERM` に加え、
既存の認証済みセッション（system keyring / dbus secret service）へ子プロセスが到達できるよう
`DBUS_SESSION_BUS_ADDRESS` と `XDG_RUNTIME_DIR` を環境変数 allowlist に追加している。

- `DBUS_SESSION_BUS_ADDRESS`（例: `unix:path=/run/user/1000/bus`）と `XDG_RUNTIME_DIR`（例: `/run/user/1000`）は
  いずれも Unix ソケットパス／ソケットディレクトリパスという *接続先エンドポイント* の値であり、
  credential 値（token 文字列・cookie・鍵の内容）そのものではない
- 上記2変数の追加後も `HOME` / `XDG_CONFIG_HOME` / `XDG_CACHE_HOME` / `XDG_STATE_HOME` は
  isolated tmp workspace 配下へ差し替えたまま維持しており（#1705 の secret-hygiene 設計の根幹は不変）、
  `dbus_session_bus_present` / `xdg_runtime_dir_present`（上記 `auth_diagnostics_metadata`）が
  `true` になった状態で isolated workspace 内から到達性が確認できることを
  `test_agy_permission_policy_env_allowlist.py` の hermetic テスト（モック化した dbus/keyring エンドポイント）で回帰確認する

### `materialize_isolated_agy_workspace()` の gcloud ADC 露出（Issue #1730、superseded_by: #1779, 2026-07-26）

> **注意（#1779 による再検証日時: 2026-07-26）**: `gcloud_adc_path` / `GOOGLE_APPLICATION_CREDENTIALS` は
> #1779 の `AGY_AUTH_PROFILE_MINIMAL`（既定）ではもはや既定で露出されない。
> `AGY_AUTH_PROFILE_EXTENDED` を明示指定した場合のみ、以下の記述どおりに動作する（実装は維持）。

#1726 で `DBUS_SESSION_BUS_ADDRESS` / `XDG_RUNTIME_DIR` を到達性変数として追加した後も、
#1494 の live fan-out 実行では全 subtask が `failure_class: "agy_auth_required"` で失敗し続けた。
原因調査の結果、実行環境の `agy` 認証キャッシュは dbus secret-service ではなく、
`$HOME/.config/gcloud/application_default_credentials.json` / `$HOME/.config/gcloud/access_tokens.db`
（gcloud Application Default Credentials、ファイルベース）に依存していることが判明した。

- `materialize_isolated_agy_workspace()` は、実行環境の `$HOME/.config/gcloud` ディレクトリが存在する場合、
  そのディレクトリを isolated workspace の `XDG_CONFIG_HOME` 配下（`<isolated>/xdg-config/gcloud`）へ
  symlink として read-only に露出する（`_expose_gcloud_adc_read_only()`）
- symlink の作成はパス文字列の書き込みのみであり、実装コード自身はファイル内容を一切 open/read しない
  （`Path.is_dir()` / `Path.is_file()` などの存在確認とパス操作のみ、Issue #1730 AC1/AC5）
- 露出される範囲は `$HOME/.config/gcloud` 配下のみであり、isolated `HOME` 自体や `.ssh` / `.netrc` /
  他の `.config/*` アプリなど、他の実 `$HOME` 配下ディレクトリは一切露出されない（AC3）
- `GOOGLE_APPLICATION_CREDENTIALS` が実行環境に設定済みの場合は、そのパス文字列（credential 値ではない）
  をそのまま isolated workspace の env へ透過する（AC2）
- 上記変更後も `HOME` / `XDG_CACHE_HOME` / `XDG_STATE_HOME` の isolated tmp workspace への差し替えは
  維持されており（#1705 の secret-hygiene 設計の根幹は不変）、tool deny 機構（no_tools/local_asset_research
  全 deny、grounded_research allowlist 限定）も影響を受けない（AC4）ことを
  `test_agy_permission_policy_gcloud_adc.py` の hermetic テストで回帰確認する
- `preflight_agy.py` の `_build_auth_diagnostics()` は `_detect_gcloud_adc()` により
  `$HOME/.config/gcloud` の存在確認レベルの検出を行い、D-Bus セッションバスが存在しない環境でも
  `auth_mode: "gcloud_adc_file_based"` を報告できる（AC7、`test_preflight_agy_gcloud_adc.py` で回帰確認）
- live AGY 実行（`run_fanout()`）が実際に gcloud ADC を使って成功することの動作確認は、本 hermetic 変更の
  スコープ外であり、#1494 の最終 E2E run に委ねる

### `materialize_isolated_agy_workspace()` の agy 独自 OAuth トークンファイル露出（Issue #1740、read-only 境界の真正性は superseded_by: #1779, 2026-07-26）

> **注意（#1779 による再検証日時: 2026-07-26）**: 露出そのもの（`agy_oauth_token_path`、
> `auth_profile` に関わらず無条件）は変更していない -- `AGY_AUTH_ABLATION_V1` で認証成功に
> 必要十分と実証済みの唯一の surface だからである。変更したのは「read only」という *主張の真正性* のみ:
> 本セクション下部の「symlink として read-only に露出する」という記述は、実際には OS レベルの強制を
> 伴わない到達可能性の付与にすぎなかった（`AGY_READONLY_BOUNDARY_V1`）。上の
> 「`agy_oauth_token_readonly_mode`: read-only 境界の真正性」セクションを参照。

#1730 で gcloud ADC 到達性（`$HOME/.config/gcloud` の read-only 露出）を追加した後も、
#1494 の live fan-out 実行（3 回目の試行）では依然として全 subtask が
`failure_class: "agy_auth_required"` で失敗し続けた。3 回目の詳細診断（read-only 存在確認・
symlink 到達性検証のみ、値は一切読んでいない）で、以下が確定した。

1. 隔離 workspace + as-shipped env → `agy_auth_required`
2. 隔離 workspace + `GOOGLE_APPLICATION_CREDENTIALS` を実 gcloud ADC ファイルパスへ明示設定
   → それでも `agy_auth_required`（**#1730 の前提は誤りだったと判明**: `agy` は gcloud ADC /
   `GOOGLE_APPLICATION_CREDENTIALS` を一切参照していない）
3. 隔離 workspace + `$HOME/.gemini/antigravity-cli/antigravity-oauth-token`（mode 600）を
   read-only symlink として露出 → 成功（`agy -p "..."` が exit_code 0 で応答）

実行環境の `agy`（Antigravity CLI）は独自の OAuth トークンファイル
`$HOME/.gemini/antigravity-cli/antigravity-oauth-token` を認証に使用しており、
dbus secret-service（#1726 の到達性追加）でも gcloud ADC（#1730 の到達性追加）でもなかった。
#1726 / #1730 で追加した dbus / gcloud ADC 到達性変数・symlink 自体は、将来別環境で必要になる
可能性があるため削除せず維持する（`DBUS_SESSION_BUS_ADDRESS` / `XDG_RUNTIME_DIR` /
`_expose_gcloud_adc_read_only()` はそのまま）。

- `materialize_isolated_agy_workspace()` は、実行環境の
  `$HOME/.gemini/antigravity-cli/antigravity-oauth-token` ファイルが存在する場合、
  そのファイルを isolated workspace 自身の `HOME` 配下
  （`<isolated HOME>/.gemini/antigravity-cli/antigravity-oauth-token`）へ symlink として
  read-only に露出する（`_expose_agy_oauth_token_read_only()`）。**Issue #1743**:
  #1740 の初期実装は誤って isolated workspace の `XDG_CONFIG_HOME` 配下
  （isolated workspace の `XDG_CONFIG_HOME` 配下の `antigravity-cli/antigravity-oauth-token` サブパス）へ配置しており、
  実際の `agy` バイナリはこのファイルを `$HOME/.gemini/antigravity-cli/` から直接読むため、
  この配置先の symlink 自体は作成に成功しても `agy -p` は引き続き `agy_auth_required` で
  失敗していた。#1494 の 4 回目の live fan-out 試行時の control-plane 診断（値は一切読まず、
  symlink 到達性のみで検証）で、配置先を isolated `HOME`（`$ISOLATED_HOME/.gemini/antigravity-cli/`、
  `agy` 自身が生成する state directory 構造と一致）に変更すれば `agy -p` が成功することを確認し、
  #1743 で `_expose_agy_oauth_token_read_only()` の配置先ロジックを修正した
  （`_expose_gcloud_adc_read_only()`（Issue #1730、gcloud ADC 側）は `XDG_CONFIG_HOME` 配下の
  ままで変更していない -- gcloud ADC は本来 `$XDG_CONFIG_HOME/gcloud` 規約に従うため）
- symlink の作成はパス文字列の書き込みのみであり、実装コード自身はファイル内容を一切 open/read しない
  （`Path.is_file()` などの存在確認とパス操作のみ、Issue #1740 AC1/AC3）
- 露出される範囲は `$HOME/.gemini/antigravity-cli/antigravity-oauth-token` の 1 ファイルのみであり、
  isolated `HOME` 自体や `.ssh` / `.netrc` / 同ディレクトリ内の他ファイル
  （`jetski_state.pbtxt` / `history.jsonl` / `settings.json`、いずれも実行環境で存在確認済みだが
  今回は露出対象外と判断した）など、他の実 `$HOME` 配下ファイル・ディレクトリは一切露出されない（AC4）。
  トークンファイル単体の露出のみで `agy -p` の成功が確認できたため、secret-hygiene 設計の趣旨
  （Issue #1705）を維持する最小露出とした
- 上記変更後も `HOME` / `XDG_CACHE_HOME` / `XDG_STATE_HOME` の isolated tmp workspace への
  差し替えは維持されており（#1705 の secret-hygiene 設計の根幹は不変）、tool deny 機構
  （no_tools/local_asset_research 全 deny、grounded_research allowlist 限定）も影響を受けない
  （AC5）ことを `test_agy_permission_policy_oauth_token.py` の hermetic テストで回帰確認する
- `preflight_agy.py` の `_build_auth_diagnostics()` は `_detect_agy_oauth_token()` により
  `$HOME/.gemini/antigravity-cli/antigravity-oauth-token` の存在確認レベルの検出を行い、
  `auth_mode: "agy_oauth_token_file_based"` を、gcloud ADC / dbus 推定より優先して報告する
  （AC9、`test_preflight_agy_oauth_token.py` で回帰確認）。gcloud ADC 推定（`gcloud_adc_file_based`）
  は agy OAuth トークンファイルが存在しない場合のフォールバックとして維持する
- live AGY 実行（`run_fanout()`）が実際に agy OAuth トークンファイルを使って成功することの
  動作確認は、本 hermetic 変更のスコープ外であり、#1494 の最終 E2E run に委ねる

## Live Evidence 保存方針 / 証跡保存ルール

`docs/dev/agy-cli-contract-20260701.md` は手書きメモではなく、sanitized `preflight_agy.py --json` 出力を要約する一次証跡として維持する。
少なくとも次の machine-readable 項目を残す。

- `schema`: 証跡 JSON の schema 名
- `ok`: preflight の最終成否
- `agy.version`: 実際に検出した agy version
- `help.noninteractive_flags`: `-p` / `--print` / `--prompt` の検出結果
- `smoke.exit_code`: smoke 実行の終了コード
- `smoke.stdout_sample`: sentinel の観測結果
- `smoke.failure_class`: fail-closed 時の分類
- `tty_condition`: 証跡取得時の TTY 条件
- `redaction_policy`: redact 方針の要約

```bash
# CI 環境での確認例（sentinel exact match）
EXPECTED="LOOP_AGY_SMOKE_OK"
ERR_FILE="$(mktemp)"
OUTPUT="$("${AGY_BIN:-agy}" -p "Return exactly: ${EXPECTED}" 2>"${ERR_FILE}")"
EXIT_CODE=$?

if [ "${EXIT_CODE}" -ne 0 ]; then
  echo "FAIL: agy exited with ${EXIT_CODE}"
elif [ "${OUTPUT}" != "${EXPECTED}" ]; then
  echo "FAIL: agy_output_mismatch (got: ${OUTPUT})"
else
  echo "OK: agy responded with expected sentinel"
fi
```

## delegation_audit_v1: 監査ログ / Delegation Audit Log

`run_gemini_headless.py` の全実行に対して、`delegation_audit_v1` という専用の closed schema を持つ
UTF-8 JSON Lines（JSONL）監査ログを出力できる。既存の `--output-file` / `--output-format json|ndjson` /
stdout / stderr の結果ストリームとはファイルレベルで完全に分離されており、`delegation_result/v1` の
契約を一切変更しない（Issue #1272）。

### 有効化方法（明示指定のみ / 暗黙有効化しない）

以下のいずれかを明示指定した場合にのみ監査ログが有効になる。指定がなければ何も書き込まれない。

```bash
# CLI フラグで指定
uv run python3 .claude/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py \
  --request-file <request.json> \
  --output-file <result.json> \
  --audit-log tmp/delegation-audit.jsonl

# 環境変数で指定（CLI フラグが優先される）
export DELEGATION_AUDIT_LOG_PATH=tmp/delegation-audit.jsonl
uv run python3 .claude/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py \
  --request-file <request.json> \
  --output-file <result.json>
```

### レコード構造 / JSONL format

1 行 1 JSON object、append-only。`run_delegation()` の 1 回の呼び出し（トップレベル呼び出しのみ。
`provider=auto` のフォールバック内部で再入する呼び出しは監査を再発行しない）につき、同一 `run_id` を
持つ `record_type: "start"` レコードが 1 件、`record_type: "end"` レコードが 1 件、必ずペアで出力される。

start / end とも `schema` フィールドは `delegation_audit_v1` に固定される。start は
`provider_requested` / `tool_profile` を必須キーとして持ち、end は `ok` / `failure_class` /
`failure_reason` / `actual_model` / `tool_profile` を必須キーとして持つ。上記以外のキーは
record_type ごとの許可済みキー集合に含まれるオプションキーのみで、それ以外のキーが混入した
レコードは closed schema 違反として拒否される（`validate_delegation_audit_record()`）。

### 秘匿情報 masking 方針 / redaction policy

監査ログに書き込まれる全ての文字列値は、既存の `_redact_text()` / `_CREDENTIAL_REGEX` による
credential masking に加えて、`$HOME` 配下の絶対パスと repo 絶対パスをそれぞれ `<HOME>` /
`<REPO_ROOT>` に置換する（`_audit_mask_text()`）。raw prompt・raw credential・raw transcript・
HOME path・repo absolute path はいずれも監査ログに出力されない。

**redaction-before-truncate**: `failure_reason` は masking を適用した後に 500 文字へ切り詰める
（`_audit_prepare_failure_reason()`）。切り詰めを先に行うと credential の断片が正規表現の
検出範囲外に残ってしまう可能性があるため、順序は固定である。

`grounded_research` の `grounding_transcript_evidence` / `citation_evidence` のような raw evidence
フィールドは監査ログに一切含めない（`grounded_metadata` は `grounding_status` /
`grounding_backend` / 各種 count / `grounding_failure_class` など public-safe な subset のみ）。

### audit failure policy（監査書き込み失敗時の挙動）

監査ログの書き込み自体が失敗した場合（ディスク書き込みエラー等）、デフォルトでは
delegation 本体の成否には一切影響しない（best-effort。stderr に warning を出力するのみ）。

`DELEGATION_AUDIT_REQUIRED=1` を明示指定した場合のみ fail-closed になり、監査ログの
書き込み失敗（または record 自体が schema 違反で構築できない場合）は例外として上位に伝播する。

```bash
# 監査書き込み失敗を fail-closed 扱いにする（オプトイン）
export DELEGATION_AUDIT_REQUIRED=1
```

### field-to-metric mapping / フィールドと監視指標の対応

| audit フィールド | 由来 | 用途 |
|---|---|---|
| `run_id` | 呼び出しごとに生成される UUID4 hex | start/end のペアリングキー |
| `provider_requested` / `tool_profile` | request の該当フィールドをそのまま記録 | 監視ダッシュボード上の provider / profile 別集計軸 |
| `ok` / `failure_class` / `failure_reason` | `delegation_result/v1` の同名フィールド（failure_reason は masking + truncate 済み） | 成功率・失敗クラス分布の集計 |
| `selected_provider` / `provider_attempts` / `fallback_reason` / `fallback_policy_version` / `attempts_by_model` | `provider_auto_policy_v1`（#1270）の `PROVIDER_AUTO_RESULT_FIELDS` と同一の集合。`provider_attempts[].failure_reason` も同じ masking + truncate を適用 | provider=auto のフォールバック発生率・provider 別成功率の監視 |
| `model_downgrades` | Gemini モデルチェーンのダウングレード履歴 | モデルダウングレード発生率の監視 |
| `post_result.post_allowed` / `post_result.post_target_type` / `post_result.request_success` / `post_result.posting_success` / `post_result.post_result` / `post_result.post_failure_class` | `post_to_issue_url` request と `post_request_success` / `post_posting_success` / `post_result` / `post_failure_class` から導出。`provider=agy` + `post_to_issue_url` の禁止経路は `post_allowed: false` / `post_target_type: issue_only` / `post_result: forbidden` / `post_failure_class: agy_post_to_issue_url_forbidden` を監査 end record に残す | post_to_issue_url 経路の許可/拒否、request 成功率、posting 成功率を別軸で監視 |
| `grounded_metadata` | `grounded_research_evidence` の public-safe subset（Issue #1266） | grounded_research の web grounding 成功率・citation 数の監視 |
| `local_asset_metadata` | `local_asset_research` プロファイル使用時の `context_files_count` / Serena retrieval 失敗フラグ | local_asset_research（Serena 経由）の失敗率監視 |
| `auth_diagnostics_metadata` | AGY の認証系 `failure_class`（`agy_auth_required` / `agy_permission_denied`）から導出（Issue #1267 territory） | 認証起因の失敗率監視 |
| `parent_run_id` / `subtask_id` / `attempt_id` | Issue #1273（fan-out）向けの予約フィールド。request に指定があれば伝播、無ければ出力されない | 将来の並列実行 orchestrator が subtask を親 run に紐付けるための予約領域 |

## Fan-Out Orchestrator: 並行実行が portability に与える影響 / Fan-Out concurrency portability notes

（Issue #1273）`fan_out_orchestrator.py` は各 subtask を独立した subprocess worker として実行する
（thread pool ではなく OS process 単位。理由: `gemini` / `agy` provider の CLI 実行そのものが
subprocess ベースであり、overall timeout 到達時にプロセスグループ単位で確実に終了させる必要があるため）。

- **プロセス生成コスト**: subtask 1 件につき Python インタプリタの起動コストが発生する（数十〜数百 ms
  オーダー）。`max_subtasks` / `max_workers` を大きくしすぎると、単純な逐次実行より起動オーバーヘッドが
  支配的になり得る。深い調査を伴わない軽量 subtask を大量 fan-out する用途には不向き。
- **プロセスグループ終了 (process group termination)**: `overall_timeout_sec` 到達時の子プロセス終了は
  `start_new_session=True`（POSIX: 新しい session/process group を子プロセスに割り当てる）と
  `os.killpg()`（SIGTERM → grace period 後 SIGKILL）に依存する。この機構は POSIX（Linux / WSL2 / macOS）
  前提であり、Windows ネイティブでは `os.killpg` / `os.getpgid` が利用できない（`NotImplementedError`
  相当）。Windows ネイティブからの fan-out 実行は Out of Scope（下記参照）。
- **OS 差異**: WSL2 上の Linux プロセスモデルでの検証を前提とする。macOS でも POSIX process group
  機構自体は利用できるが、本 skill の対応環境（Supported Environments 参照）に含まれないため未検証。
- **リソース上限**: `max_workers` / provider 別・profile 別 semaphore は同時に起動する OS プロセス数を
  直接制限する。WSL2 環境のメモリ・ファイルディスクリプタ上限を踏まえ、既定値（`max_workers=4`）から
  大きく外れる設定を行う場合は実行環境のリソース余裕を事前に確認すること。
- **NDJSON journal の並行書き込み**: parent プロセス内の単一 writer が `os.O_APPEND` + 単一 `write()`
  syscall で 1 record = 1 write を行う。この atomicity は POSIX の正規ファイルへの `O_APPEND` 書き込みに
  依存しており、NFS 等の一部ネットワークファイルシステムでは atomicity 保証が弱まる可能性がある
  （本 skill の実行 artifact はローカルディスク前提であり、NFS 等での動作は未検証）。

## Out of Scope / 対象外

- CodexCLI 向け実行手順（Followup Issue 扱い）
- Windows PowerShell ネイティブからの直接実行
- macOS 環境
