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

### field-to-metric mapping

| audit フィールド | 由来 | 用途 |
|---|---|---|
| `run_id` | 呼び出しごとに生成される UUID4 hex | start/end のペアリングキー |
| `provider_requested` / `tool_profile` | request の該当フィールドをそのまま記録 | 監視ダッシュボード上の provider / profile 別集計軸 |
| `ok` / `failure_class` / `failure_reason` | `delegation_result/v1` の同名フィールド（failure_reason は masking + truncate 済み） | 成功率・失敗クラス分布の集計 |
| `selected_provider` / `provider_attempts` / `fallback_reason` / `fallback_policy_version` / `attempts_by_model` | `provider_auto_policy_v1`（#1270）の `PROVIDER_AUTO_RESULT_FIELDS` と同一の集合。`provider_attempts[].failure_reason` も同じ masking + truncate を適用 | provider=auto のフォールバック発生率・provider 別成功率の監視 |
| `model_downgrades` | Gemini モデルチェーンのダウングレード履歴 | モデルダウングレード発生率の監視 |
| `post_result.request_success` / `post_result.posting_success` | `post_request_success` / `post_posting_success`（Issue #1272 で追加。content 生成成功と GitHub post 成功を分離） | post_to_issue_url 経路の request 成功率 / posting 成功率を別軸で監視 |
| `grounded_metadata` | `grounded_research_evidence` の public-safe subset（Issue #1266） | grounded_research の web grounding 成功率・citation 数の監視 |
| `local_asset_metadata` | `local_asset_research` プロファイル使用時の `context_files_count` / Serena retrieval 失敗フラグ | local_asset_research（Serena 経由）の失敗率監視 |
| `auth_diagnostics_metadata` | AGY の認証系 `failure_class`（`agy_auth_required` / `agy_permission_denied`）から導出（Issue #1267 territory） | 認証起因の失敗率監視 |
| `parent_run_id` / `subtask_id` / `attempt_id` | Issue #1273（fan-out）向けの予約フィールド。request に指定があれば伝播、無ければ出力されない | 将来の並列実行 orchestrator が subtask を親 run に紐付けるための予約領域 |

## Out of Scope / 対象外

- CodexCLI 向け実行手順（Followup Issue 扱い）
- Windows PowerShell ネイティブからの直接実行
- macOS 環境
