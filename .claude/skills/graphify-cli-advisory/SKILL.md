---
name: graphify-cli-advisory
description: pinned Graphify CLI（`graphifyy==0.9.34`）を codebase-investigator の local_asset_research 前段に置く、任意・read-only な advisory 候補絞り込み層。「Graphify で候補を絞る」「graphify prefilter」のトリガーで使う。
---

# Graphify CLI Advisory (pilot)

`codebase-investigator` SubAgent の既存 `local_asset_research` 経路（`gemini-cli-headless-delegation` 経由の
Gemini + Serena MCP read-only source readback）の **前段** に置く、任意実行の Graphify CLI 候補絞り込み層。

## 位置づけ（Non-Goals を含む）

- Graphify は **advisory prefilter** に留まる。SSOT・planner・executor gate・review authority・
  Allowed Paths producer・CI gate のいずれにもならない。
- Graphify 単独では finding を確定しない。候補絞り込み後は **必ず** 既存 Gemini `local_asset_research` と
  Serena source confirmation を最終確認経路として実行する。
- Graphify の stdout・node ID・community ID・confidence label（`EXTRACTED` / `INFERRED`）は候補抽出用の
  一時情報にすぎない。Graphify の結果単独では finding を確定しない。既存の `CODEBASE_INVESTIGATION_RESULT_V1` と
  `REPO_EVIDENCE_REF_V1` を最終出力の SSOT とし、新しい repo-wide schema（`GRAPHIFY_CONTEXT_V1` /
  `GRAPHIFY_RESULT_V1` / `GRAPHIFY_EVIDENCE_V1`、専用 schema registry entry、専用 digest・freshness authority）
  は追加しない。
- Markdown / `docs/dev/**` の semantic extraction、SKILL.md 契約の semantic coverage、PDF/Office 文書、
  Agent→Skill→docs SSOT の完全な関係図は **claim しない**（本 skill の scope 外）。

## Package / CLI 起動契約

- exact pinned package spec: `graphifyy==0.9.34`（floating version は使わない）。`pyproject.toml` /
  `uv.lock` / project `.venv` は一切変更しない。都度 `uvx --from graphifyy==0.9.34 graphify <subcommand>`
  で isolated tool env を使う。
- `graphify --version` の readback を必ず結果に含める。

## Allowlisted subcommand（許可された subcommand、`_ALLOWED_SUBCOMMANDS`）

```
graphify extract <path> --code-only --out tmp/graphify/<head-sha>/
graphify query "<question>" --budget N [--graph <path>]
graphify path "A" "B" [--graph <path>]
graphify explain "X" [--graph <path>]
graphify --version
```

**上記以外のあらゆる subcommand（`install` / `install --strict` / `uninstall` / `hook install` /
`watch` / `prs` / `prs --triage` / `prs --conflicts` / `graphify-mcp` / `python -m graphify.serve` /
MCP server 登録 / `.agents/mcp_config.json` への追加 / git hook / merge driver / required CI check 等）は
wrapper が起動前に拒否する。** `run_graphify_cli_advisory.py` の `_ALLOWED_SUBCOMMANDS` がこの allowlist の
唯一の正本であり、`_FORBIDDEN_TOKENS` は防御的な検証・テスト用の参考リストにすぎない。

## clean worktree gate（未追跡差分がない状態のみ許可する検証ゲート）

Graphify graph の新規作成・利用は **clean worktree の場合だけ** 行う。dirty worktree
（`git status --porcelain` が非空、またはその判定自体が失敗する場合を含む fail-closed）が検出された場合、
graph の新規作成・利用のいずれも行わず `status: unavailable` / `reason: dirty_worktree` を返し、既存
Gemini/Serena 調査は継続する（Graphify skip は調査全体の hard failure にしない）。HEAD SHA だけで dirty
worktree を表現できるとは主張しない。

## 出力先の制限

Graphify 出力は `tmp/graphify/<head-sha>/` 配下（未追跡領域、`docs/dev/repository-folder-policy.md` の
`REPOSITORY_FOLDER_POLICY_V1` に準拠）にのみ書く。`graphify-out/` や `.graphify/` を repo 直下やその他の
場所に生成しない。wrapper は呼び出し元から出力先パスの override を受け付けず、`repo_root` と `head_sha`
から常に自前で `tmp/graphify/<head-sha>/` を導出する（path escape 対策）。

## query logging（クエリログ制御）

query logging は既定 off。wrapper は `GRAPHIFY_QUERY_LOG_DISABLE=1` を常に設定し、
`GRAPHIFY_QUERY_LOG_ENABLE` / `GRAPHIFY_QUERY_LOG` が inherited env に存在してもこれらを pop してから
disable フラグを設定する（enable 系より disable が優先される upstream 仕様に対する防御的多重化）。

## bounded budget（出力上限の制御）

`graphify query` 実行時は必ず `--budget` を明示する（既定 500 token）。無制限出力を渡さない。

## non-zero exit の分類

wrapper は launch 失敗・non-zero exit・timeout・missing graph・dirty worktree・disallowed subcommand の
いずれについても例外を送出せず、常に `GraphifyAdvisoryResult(status="unavailable", reason=<...>)` を返す。
呼び出し元は Graphify が unavailable でも例外なく既存調査経路（Gemini local_asset_research / Serena
source confirmation）へ fallback する。Graphify の結果単独では finding を確定しない。

## Wrapper 実装（`scripts/run_graphify_cli_advisory.py`）

- `subprocess` は list argv で起動する（`shell=True` を使わない）。
- repository root 検証・clean worktree 検証・exact package spec 固定・version readback・output path
  制限・query log 無効化・query output bounded budget・timeout 設定・stdout/stderr 分離・非破壊 fallback
  をすべて実装する。
- `RunnerFn` を injectable にしている（`runner: RunnerFn = _default_runner`）。テストは fake runner を
  注入し、PyPI／ネットワークに一切依存しない決定論的な検証を行う（`tests/test_run_graphify_cli_advisory.py`）。

## Known Limitations（既知の制約）

- exact top-level version pin（`graphifyy==0.9.34`）は transitive dependency（`numpy` / `rapidfuzz` 等）
  全体の lock ではない。tool 専用 lock ファイルや required CI 化は本 skill の scope 外とし、後続判断に
  委ねる。
- dirty worktree fingerprint schema の新設は行わない。dirty 判定は毎回 `git status --porcelain` 相当の
  実チェックで行う。

## codebase-investigator との関係

`.claude/agents/codebase-investigator.md` は本 skill を **任意** の prefilter として言及するのみで、CLI
手順の詳細（subcommand・flag 一覧等）は埋め込まない（詳細はこの SKILL.md を参照させる）。Graphify
prefilter 後も既存の Gemini `local_asset_research` と Serena source confirmation を必ず実行する旨を
明記する。

## Related（関連情報）

- `.claude/agents/codebase-investigator.md` — 本 skill を任意で呼び出す SubAgent
- `.claude/skills/gemini-cli-headless-delegation/SKILL.md` — 最終確認経路（Gemini + Serena MCP）
- `docs/dev/repository-folder-policy.md` — `tmp/` の repo-approved temporary workspace 定義
- `.claude/skills/graphify-cli-advisory/scripts/run_graphify_cli_advisory.py` — wrapper 実装
- `.claude/skills/graphify-cli-advisory/tests/test_run_graphify_cli_advisory.py` — 決定論的テスト
