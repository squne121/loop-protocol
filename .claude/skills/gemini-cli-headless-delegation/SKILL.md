---
name: gemini-cli-headless-delegation
description: Gemini CLI を wrapper 経由で非対話 delegation する shared skill。巨大ログ調査、構造化された技術調査、根拠付き比較を構造化 request 契約で委譲したいときに使う。
disable-model-invocation: true
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
   - `trusted_folders` チェックはリポジトリルートを `~/.gemini/trustedFolders.json` に**書き込む**（read-only ではない）。既登録時は no-op（idempotent）。`--fix` による check / write 分離は #313 で対応予定。
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

   `failure_class: "trusted_workspace_required"` の場合は `--skip-trust` 対応の Gemini CLI バージョンを確認するか `GEMINI_CLI_TRUST_WORKSPACE=true` でフォールバックする（詳細は `references/runtime-portability.md` 参照）。

2. **`delegation_request_v1` JSON を作る**（`references/usage-contract.md` の「Request Contract」を参照）。

3. `scripts/run_gemini_headless.py` で request を検証・実行する（wrapper は `context_files` を読み込む。`no_tools` / `grounded_research` は isolated temp cwd から Gemini を起動し、`local_asset_research` は machine-checkable な Serena MCP read-only 設定を検証できた場合だけ repo root から起動する）。

4. **stdout から最終報告を確認する**:
   - `ok: true` 時: 後方互換のため `response_text` の内容が stdout に出力される。
   - `ok: true` でも `response_text` が空の場合: `[gemini-headless] warning: response_text is empty` が出力される。
   - `ok: false` 時: 失敗理由（`warnings[0]`）が stdout に出力される。
   - 常に `[gemini-headless] result saved to: <output-file のパス>` が stdout に出力される。
   - orchestrator は `--output-file` の JSON から `result_surface.summary` / `result_surface.primary_artifact` / `result_surface.next_action` を優先して参照し、詳細な long-form evidence が必要な場合だけ `response_text` を読む。

5. **（オプション）`post_to_issue_url` で GitHub へコメント投稿する**: wrapper 側の書き込み操作（Gemini 側ではない）。`ok: true` かつ `response_text` 存在時に自動投稿し、`result.json` に `comment_url` が追加される。許可 profile・失敗時の warnings 処理は `references/usage-contract.md` 参照。orchestrator は `result_surface.primary_artifact` / `result_surface.summary` を先に参照する。

## Core Rules

### Delegation Boundary
- Gemini 側の `file write` / `shell edit` / GitHub mutation は禁止。
- `post_to_issue_url` は **wrapper 側**の GitHub コメント投稿操作（Gemini 側ではない）。`no_tools` / `grounded_research` のみ許可（`local_asset_research` / `proposal_only` / `github_research` では拒否）。詳細は `references/usage-contract.md` 参照。
- 最終 file edit / shell 実行 / GitHub mutation は caller / orchestrator が保持する。

### Request Validation
- `tool_profile` は必ず明示し、暗黙既定を置かない。有効値は `no_tools` / `grounded_research` / `local_asset_research` / `proposal_only` / `github_research` のみ。
- `context_files` が欠ける、参照先が見つからない、`objective` が曖昧すぎる、`instructions` が 2 件未満、`output_sections` が空なら fail-closed にする。
- `--output-format` / `stream-json` はこの skill の既定経路では扱わない。`result.json` ベースの headless JSON 契約を前提にする。
- 実行モードは `--approval-mode plan` を既定とし、read-only 前提を崩さない。
- `GEMINI.md` など ambient context を避けるため、wrapper は isolated temp cwd で Gemini を呼ぶ。
- stderr は破棄せず `warnings` と `stderr` に保持する。

### Model Routing
- モデル選択・降格チェーン・quota retry・role 別優先列の詳細は `references/model-routing.md` を参照。
- 明示 `model` 指定時は降格せずその model のみで試行する。

## References
- `references/model-routing.md`（model routing スキーマ・role テーブル・reason_code 一覧・解決順）
- `references/provider-mapping.md`
- `references/result-surface.md`
- `references/usage-contract.md`（リクエスト JSON フィールド仕様・profile ルール・gh_commands 仕様の正本）
- `references/runtime-portability.md`（Claude Code WSL2 からの実行手順）
- `references/isolated-cwd-rationale.md`（isolated temp cwd の設計根拠）
- `references/delegation-task-classes.md`（R0/R1/R2/R3 定義と評価マトリクス）
- `config/model_routing.yaml`（任意オーバーライド設定ファイル）

## 出力制約 (OUTPUT_BUDGET_V1)

`docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` の制約に従う。routing-critical な機械可読フィールドは削らず、人間向け説明・証跡・diff 再掲のみを削減する。
