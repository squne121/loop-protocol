# Runtime Portability

## Supported Environments

| 環境 | 状態 | 備考 |
|------|------|------|
| Claude Code on WSL2 (Ubuntu 24.04) | primary | 主要サポート対象 |
| Codex CLI | Followup Issue 対応 | 未検証・Out of Scope |

## Prerequisites

- Node.js 18+ がインストール済みであること（`node --version` で確認）
  - `gemini` CLI は `@google/gemini-cli` Node.js パッケージとして提供される
  - nvm 経由のインストールを推奨: `nvm install --lts`
- `gemini` CLI がインストール済みであること（`gemini --version` で確認）
  - インストール方法: `npm install -g @google/gemini-cli`
- Python 3.10+ が利用可能であること
- `uv` が利用可能であること（テスト実行用）
- Google アカウントによる認証が完了していること

## Execution from Claude Code (WSL2)

### 1. Preflight で環境確認

```bash
uv run python3 .claude/skills/gemini-cli-headless-delegation/scripts/preflight_gemini_headless.py \
  --output-file tmp/gemini-headless-preflight.json
cat tmp/gemini-headless-preflight.json
```

### 2. Request JSON を作成

`delegation_request_v1` スキーマで request ファイルを作成する（`references/usage-contract.md` の Request Contract 参照）。

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

### 3. Wrapper 実行

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

## Trusted Folder の programmatic 登録

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

**恒久対応は parent Issue #104 の agy 移行**である。
API key 暫定運用は #104 の agy provider 実装が完了したら不要になる。

- agy 移行の進捗は #104 を参照。
- agy が利用可能になったら `gemini-cli-headless-delegation` skill の provider を切り替える。

---

## agy Provider: 実行ポータビリティ

### AC4: agy -p Manual Smoke 手順

`agy -p`（`--print` / `--prompt` フラグ）を使った手動動作確認の手順。

> 実機証跡生成および証跡の保存は #109 / #110 または別 Issue の責務。
> 本手順は docs-only（手順文書化のみ）であり、自動実行や証跡保存は対象外。

```bash
# 1. agy が利用可能か確認
${AGY_BIN:-agy} --version

# 2. isolated temp cwd を作成
TMPDIR=$(mktemp -d)

# 3. agy -p で簡単な prompt を実行（no_tools 相当）
cd "$TMPDIR" && ${AGY_BIN:-agy} -p "Hello, respond with 'ok'"

# 4. 終了コードと stdout を確認
# exit 0 かつ stdout に何らかの出力があれば smoke ok
# exit 0 かつ stdout が空の場合は fail-closed（後述の AC9 参照）
```

`setup_check.py --provider agy` は現時点では未実装（Followup Issue 対応）。

### AC5 / AC10: AGY_BIN override と precedence

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

#### raw_command 表示時の secret/path leakage 回避

`AGY_BIN` の値をログ・stdout・エラーメッセージに出力する場合は以下に注意する。

- `AGY_BIN` に secret や認証情報を含めない（バイナリパスのみ設定する）
- raw_command をログに記録する場合は `$AGY_BIN` の値をそのまま出力してよいが、
  コマンドライン引数に含まれる prompt text は必要に応じてトランケートする
- path に home directory (`~` / `$HOME`) が含まれる場合は展開後の絶対パスを記録する
  （環境依存のパス解決を避けるため）

### AC9: non-TTY / pipe / CI 環境での fail-closed

`agy -p` を non-TTY 環境（pipe / CI / headless 実行）で呼び出した場合、以下の挙動に注意する。

| 状態 | 判定 | エラーコード |
|------|------|------|
| exit 0 かつ stdout に出力あり | ok（smoke pass） | - |
| exit 0 かつ stdout が空 | fail-closed | `agy_empty_stdout` |
| exit 0 かつ stdout が空（CI 環境） | fail-closed | `agy_output_missing` |
| exit non-0 | fail-closed | exit code に応じた分類 |

non-TTY / pipe 環境で `agy -p` が exit 0 かつ stdout 空になった場合は、
agy が TTY 検出により出力を抑制した可能性があるため、**fail-closed** として扱う。
stdout が空の場合に PASS として扱う設計は禁止（silent failure を PASS に変換しない）。

```bash
# CI 環境での確認例
OUTPUT=$(${AGY_BIN:-agy} -p "Hello" 2>/dev/null)
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
  echo "FAIL: agy exited with $EXIT_CODE"
elif [ -z "$OUTPUT" ]; then
  echo "FAIL: agy_empty_stdout / agy_output_missing (exit 0 but stdout empty)"
else
  echo "OK: agy responded"
fi
```

## Out of Scope

- CodexCLI 向け実行手順（Followup Issue 扱い）
- Windows PowerShell ネイティブからの直接実行
- macOS 環境
