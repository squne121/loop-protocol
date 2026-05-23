---
name: gemini-cli-headless-delegation
description: Gemini CLI を wrapper 経由で非対話 delegation する shared skill。巨大ログ調査、構造化された技術調査、根拠付き比較を構造化 request 契約で委譲したいときに使う。
---

# Gemini CLI Headless Delegation

> **このスキルは `.claude/skills/gemini-cli-headless-delegation/` で管理される正本です。** `.agents/skills/` への参照・コピーは不要です。変更は直接このファイルを編集してください。

## Use When
- 巨大ログ、インシデントログ、ビルドログ、テスト失敗ログを委譲したい
- 構造化された技術調査や根拠付き比較を Gemini に任せたい
- `implement-issue` / `create-issue` 向けに、最終 write を caller / orchestrator 側に残したまま下書きだけを Gemini に委譲したい
- read-only headless 実行を使いたい
- caller / orchestrator から同じ wrapper を経由して Gemini を呼びたい（`2 層 delegation 経路`: caller/orchestrator -> wrapper -> Gemini）

## Do Not Use When
- 曖昧な brainstorming や「いい感じに調べて」だけの依頼
- 実装、編集、削除の代行
- multi-turn の対話セッション前提の作業
- Gemini を ad hoc に直接叩く運用
- file edit / shell edit / GitHub mutation を Gemini 側に実行させたい依頼

## リクエスト JSON

委譲リクエストの詳細なフィールド仕様・制約・有効値は `references/usage-contract.md` を参照すること（usage-contract.md が single source of truth）。

**tool_profile 早見表**（詳細は `references/usage-contract.md` 参照）:

| tool_profile | 用途 |
|---|---|
| `no_tools` | ツール不使用・コンテキスト読み取り専用 |
| `grounded_research` | Google Search grounding あり（timeout_sec: 300+ 推奨） |
| `local_asset_research` | Serena MCP read-only によるローカル資産調査 |
| `proposal_only` | 実装案・Issue 本文案・patch proposal のドラフト生成 |
| `github_research` | GitHub read-only 調査（gh コマンド allowlist）|

モデル選択・降格チェーン・role 設定は `references/model-routing.md` を参照。

返却面の優先順位は `references/result-surface.md` を正本とする。caller / orchestrator は `response_text` 全文よりも `result_surface.summary` / `result_surface.primary_artifact` / `result_surface.next_action` を先に使う。

## Workflow

> **委譲前に必ず Step 0 の setup_check を実行する。** setup_check が `ok: false` を返した場合は Step 1 の preflight に進まず先に依存を解決する。

0. **setup_check で依存ツール・trusted folder・Serena MCP・settings.json を確認する（必須）**:
   ```bash
   uv run python3 .claude/skills/gemini-cli-headless-delegation/scripts/setup_check.py --json
   ```
   - `exit_code: 0` かつ `ok: true` であれば Step 1 に進む。
   - `ok: false` の場合は JSON 出力の各チェック項目 (`tools`, `trusted_folders`, `serena_mcp`, `gemini_settings`, `auth`) を確認し、`recovery` フィールドに従って対処する。
   - `trusted_folders` チェックはリポジトリルートを `~/.gemini/trustedFolders.json` に programmatic に追記する（idempotent）。
   - `gemini_settings` チェックは `.gemini/settings.json` が不在の場合にのみ Serena MCP テンプレを生成する（既存ファイルは保護）。
   - アカウント認証（Google OAuth）は人間が事前に `gemini auth login` で完了させておく必要がある。

   テスト実行時（`tests/` 配下）:
   ```bash
   cd .claude/skills/gemini-cli-headless-delegation && uv run --with pytest --with pyyaml python -m pytest tests/
   ```

1. **preflight を実行する（必須）**:
   ```bash
   uv run python3 .claude/skills/gemini-cli-headless-delegation/scripts/preflight_gemini_headless.py \
     --output-file tmp/gemini-headless-preflight.json
   cat tmp/gemini-headless-preflight.json | uv run python3 -c "import sys,json; r=json.load(sys.stdin); print('ok:', r['ok'])"
   ```
   `ok: false` の場合は委譲を中止し、`failure_reason` を確認して修正する。

   **trusted workspace 失敗の復旧手順（`failure_class: "trusted_workspace_required"`）:**

   preflight smoke および本番委譲コマンドには `--skip-trust` が**既定で付与**されており（Issue #1824）、通常は trust エラーが発生しない。それでも出力 JSON に `failure_class: "trusted_workspace_required"` が含まれる場合は、使用中の Gemini CLI バージョンが `--skip-trust` をサポートしていない可能性がある。以下のフォールバック手順を試すこと:

   ```bash
   # フォールバック手順（--skip-trust 非対応バージョン用）
   GEMINI_CLI_TRUST_WORKSPACE=true uv run python3 \
     .claude/skills/gemini-cli-headless-delegation/scripts/preflight_gemini_headless.py \
     --output-file tmp/gemini-headless-preflight.json
   ```

   - `--skip-trust` は `preflight_gemini_headless.py` と `run_gemini_headless.py` の両方に既定で含まれる（Issue #1824）。
   - `recovery_action` フィールド（JSON 出力）および stdout の `[gemini-preflight] recovery:` 行に復旧手順が machine-readable に含まれる。
   - `~/.gemini/trustedFolders.json` は exact path matching のみ対応で wildcard 非対応のため、temp dir の信頼付与には使えない。

2. **`delegation_request_v1` JSON を作る**（`references/usage-contract.md` の「Request Contract」を参照）。

3. `scripts/run_gemini_headless.py` で request を検証・実行する（wrapper は `context_files` を読み込む。`no_tools` / `grounded_research` は isolated temp cwd から Gemini を起動し、`local_asset_research` は machine-checkable な Serena MCP read-only 設定を検証できた場合だけ repo root から起動する）。

4. **stdout から最終報告を確認する**:
   - `ok: true` 時: 後方互換のため `response_text` の内容が stdout に出力される。
   - `ok: true` でも `response_text` が空の場合: `[gemini-headless] warning: response_text is empty` が出力される。
   - `ok: false` 時: 失敗理由（`warnings[0]`）が stdout に出力される。
   - 常に `[gemini-headless] result saved to: <output-file のパス>` が stdout に出力される。
   - orchestrator は `--output-file` の JSON から `result_surface.summary` / `result_surface.primary_artifact` / `result_surface.next_action` を優先して参照し、詳細な long-form evidence が必要な場合だけ `response_text` を読む。

5. **（オプション）GitHub Issue/PR へコメント投稿する**（`post_to_issue_url` が指定された場合のみ）:
   - `post_to_issue_url` フィールドを request JSON に含める。値は投稿先の GitHub Issue / PR の URL（例: `https://github.com/owner/repo/issues/123` または `https://github.com/owner/repo/pull/456`）。
   - wrapper（`run_gemini_headless.py`）が自動的に以下を実行する:
     - Step 4 で `ok: true` かつ `response_text` が存在する場合、内部で以下のコマンドを実行する:
       ```bash
       # jq で JSON から response_text を抽出し、投稿本文を準備する
       RESPONSE=$(jq -r '.response_text' <output-file>)
       # 実際の投稿経路は wrapper / usage-contract の current 実装を参照する
       ```
     - 投稿成功時は `result.json` に `comment_url` フィールドが追加される（例: `https://github.com/owner/repo/issues/123#issuecomment-XXXXXXXXX`）。
     - 投稿失敗時は `warnings` に失敗理由が追加される。`ok` は変わらない（調査自体は成功）。
   - **オーケストレーター受取**: `result.json` の `result_surface.primary_artifact` と `result_surface.summary` を記録する。調査結果全文（`response_text`）は detail が必要な場合にだけ読む。

## Core Rules

### Delegation Boundary
- この skill の delegated 実行は `read-only / report-only / local_asset_research / proposal_only` を想定する。
- `file write` / `shell edit` / GitHub 書込操作 / `implementation write` は wrapper 契約外のまま維持する。
- 最終 file edit / shell 実行 / GitHub mutation は caller / orchestrator 側 worker または main thread が保持する。
- 詳細なフィールド制約・profile ルール・fail-closed 条件は `references/usage-contract.md` を参照。

### Request Validation
- `tool_profile` は必ず明示し、暗黙既定を置かない。有効値は `no_tools` / `grounded_research` / `local_asset_research` / `proposal_only` / `github_research` のみ。
- `context_files` が欠ける、参照先が見つからない、`objective` が曖昧すぎる、`instructions` が 2 件未満、`output_sections` が空なら fail-closed にする。
- `--output-format` / `stream-json` はこの skill の既定経路では扱わない。`result.json` ベースの headless JSON 契約を前提にする。
- 実行モードは `--approval-mode plan` を既定とし、read-only 前提を崩さない。
- `GEMINI.md` など ambient context を避けるため、wrapper は isolated temp cwd で Gemini を呼ぶ。
- stderr は破棄せず `warnings` と `stderr` に保持する。

### Model Routing
- モデル選択・降格チェーン・role 別優先列の詳細は `references/model-routing.md` を参照。
- quota 枯渇（429 / `MODEL_CAPACITY_EXHAUSTED` / `RESOURCE_EXHAUSTED`）時は `role` の `model_chain`（または `default_chain`）に沿って下位 model へ自動降格 retry する。chain を使い切ったら fail-closed し、caller-side fallback（直接生成）はその後に発動する。
- 明示 `model` 指定時は降格せずその model のみで試行する。
- `HTTP 429` / `MODEL_CAPACITY_EXHAUSTED` / `RESOURCE_EXHAUSTED` は一時的失敗として扱い、指数バックオフ付きで再試行する。chain を使い切ったら fail-closed（`reason_code: "model_chain_exhausted"`）。
- 再試行上限超過時は `retry_exhausted` 相当の構造化報告にまとめ、原因・試行回数・各遅延時間・最終状態を明示する。

## local_asset_research runtime smoke

production enablement の acceptance gate として、repo root から以下の手順で runtime smoke を実行できる。

```bash
# Step 1: preflight で静的設定と trusted workspace / OAuth を確認する
uv run python3 .claude/skills/gemini-cli-headless-delegation/scripts/preflight_gemini_headless.py \
  --compact \
  --output-file tmp/gemini-preflight.json
# ok: false → failure_reason を確認して Stop Condition として記録する
uv run python3 - <<'PY'
import json
from pathlib import Path
preflight = json.loads(Path("tmp/gemini-preflight.json").read_text(encoding="utf-8"))
assert preflight["local_asset_research"]["prompt_stdin_supported"], "long-context stdin route not available"
PY

# Step 2: local_asset_research smoke fixture を実行する
uv run python3 .claude/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py \
  --request-file .claude/skills/gemini-cli-headless-delegation/tests/fixtures/local_asset_research_smoke_request.json \
  --output-file tmp/local-asset-smoke.json \
  --compact
# ok: false は Stop Condition（外部認証要因）として記録し、実装失敗と混同しない

# Step 3: 結果を機械判定する
uv run python3 - <<'PY'
import json
from pathlib import Path
r = json.loads(Path('tmp/local-asset-smoke.json').read_text(encoding='utf-8'))
assert r.get('tool_profile') == 'local_asset_research', f"tool_profile mismatch: {r.get('tool_profile')}"
if not r.get('ok'):
    print('STOP_CONDITION: local_asset_research smoke did not pass:', r.get('failure_reason'), r.get('warnings'))
    raise SystemExit(2)
response = str(r.get('response_text') or '')
assert response.strip(), 'response_text is required for successful local_asset_research smoke'
print('PASS: local_asset_research runtime smoke returned response_text')
PY
```

成功時の機械判定基準:
- `exit_code == 0` かつ `ok == true`
- `tool_profile == "local_asset_research"`
- `response_text` が非空文字列

`ok: false` の場合は実装失敗ではなく外部認証要因（trusted workspace 未成立、OAuth credential 不足）の Stop Condition として扱い、`failure_reason` / `warnings` を記録する。

## Verification
- `python3 .claude/skills/gemini-cli-headless-delegation/scripts/preflight_gemini_headless.py --output-file tmp/gemini-headless-preflight.json`
- `python3 .claude/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py --request-file <request.json> --output-file <result.json>`
- `git diff --check`

## References
- `references/model-routing.md`（model routing スキーマ・role テーブル・reason_code 一覧・解決順）
- `references/provider-mapping.md`
- `references/result-surface.md`
- `references/usage-contract.md`（リクエスト JSON フィールド仕様・profile ルール・gh_commands 仕様の正本）
- `references/runtime-portability.md`（Claude Code WSL2 からの実行手順）
- `references/isolated-cwd-rationale.md`（isolated temp cwd の設計根拠）
- `references/delegation-task-classes.md`（R0/R1/R2/R3 定義と評価マトリクス）
- `config/model_routing.yaml`（任意オーバーライド設定ファイル）
