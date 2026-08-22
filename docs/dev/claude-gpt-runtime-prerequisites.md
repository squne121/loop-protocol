# Claude-GPT ランタイム前提条件

このドキュメントは、`scripts/claude-gpt/preflight.sh --workflow-profile issue-to-impl`
（実装は `scripts/claude-gpt/workflow_capability_preflight.py`、構造化結果は
`CLAUDE_GPT_WORKFLOW_CAPABILITIES_V1`）が判定するランタイム前提条件のうち、
恒久的な参照先として残すべき運用メモをまとめる（Issue #2273）。

## trusted uv

`checks.uv.status` が `ok` でない場合、pin された `uv` バージョンを
account-home の `~/.local/bin`（公式 standalone installer 経由）に
インストールするか、hostedtoolcache が提供する `uv` を使う。判定ロジック自体は
`scripts/agent-guards/trusted_runtime_capabilities.py` が
`scripts/agent-guards/skill_runtime_exec.py` の canonical resolver に委譲する
（新しい trust boundary は導入しない）。

## Spark delegation route の判定範囲（P1-5 責務境界）

`checks.spark.status`（`not_required` / `eligible` / `fallback_only` /
`unavailable`）は、claude-code-proxy バイナリの availability と ChatGPT
subscription auth の availability のみに基づく静的判定である。

**`spark.status: eligible` は、実行時に実際に使われるモデルが
`CLAUDE_CODE_SUBAGENT_MODEL` の override 意図と適合していることの証明では
ない。** 実効 model 適合性の検証は、PR #2285 / Issue #2274 で導入された
Agent 起動直前の model gate（`resolvedModel` ベースの判定）の責務であり、
本 preflight はそれを重複実装しない。
