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

### `provider=agy`

- `agy` CLI がインストール済みであること（`agy --version` で確認）
- Python 3.10+ が利用可能であること
- `uv` が利用可能であること（`setup_check.py` / `preflight_agy.py` 実行用）
- Node.js / `gemini` CLI / `uvx` / Serena MCP / `.gemini/settings.json` / trustedFolders / Gemini OAuth は不要
- `setup_check.py --provider agy --json` は check-only で、`.gemini/` や trustedFolders を変更しない

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

```bash
# 起動可能性を確認（--help で終了するため実際のインストールは行われない）
uvx --from git+https://github.com/oraios/serena serena-mcp-server --help
```

### `.gemini/settings.json` テンプレ

リポジトリルートの `.gemini/settings.json` が存在しない場合、`setup_check.py` が以下のテンプレを自動生成する（既存ファイルは上書きしない）:

```json
{
  "mcp": {
    "allowed": ["serena"]
  },
  "mcpServers": {
    "serena": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/oraios/serena",
        "serena-mcp-server",
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
      ]
    }
  }
}
```

この設定は `preflight_gemini_headless.py` の `_validate_local_asset_research_settings` が要求する条件と完全に一致している。

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

## Out of Scope / 対象外

- CodexCLI 向け実行手順（Followup Issue 扱い）
- Windows PowerShell ネイティブからの直接実行
- macOS 環境
