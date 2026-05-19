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
uv run python3 .agents/skills/gemini-cli-headless-delegation/scripts/preflight_gemini_headless.py \
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
uv run python3 .agents/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py \
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

## Out of Scope

- CodexCLI 向け実行手順（Followup Issue 扱い）
- Windows PowerShell ネイティブからの直接実行
- macOS 環境
