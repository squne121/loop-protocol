# Claude-GPT ランタイム前提条件

このドキュメントは、`scripts/claude-gpt/preflight.sh --workflow-profile issue-to-impl`
（実装は `scripts/claude-gpt/workflow_capability_preflight.py`、構造化結果は
`CLAUDE_GPT_WORKFLOW_CAPABILITIES_V1`）が判定するランタイム前提条件のうち、
恒久的な参照先として残すべき運用メモをまとめる（Issue #2273）。

## trusted uv（信頼済み uv バイナリの利用）

`checks.uv.status` が `ok` でない場合、pin された `uv` バージョンを
account-home の `~/.local/bin`（公式 standalone installer 経由）に
インストールするか、hostedtoolcache が提供する `uv` を使う。つまり、
未信頼なパスに存在する `uv` バイナリを preflight が誤って許可しないよう、
インストール元を account-home 配下の `~/.local/bin` か、
CI ランナーが提供する hostedtoolcache のいずれかに限定している。
判定ロジック自体は `scripts/agent-guards/trusted_runtime_capabilities.py` が
`scripts/agent-guards/skill_runtime_exec.py` の canonical resolver に委譲しており、
このドキュメントの目的のために新しい trust boundary を追加で導入するものではない。

詳細な探索lane・version pinの正規化・復旧コマンドは `docs/dev/workflow.md` の
「Trusted uv のローカル開発復旧」を正本とする。

## Spark delegation route: 撤去済み（Issue #2651）

GPT-5.3-Codex-Spark delegation は repository-owned Claude-GPT / Claude Code
integration から撤去された。`checks.spark.status` はかつて
`not_required` / `eligible` / `fallback_only` / `unavailable` の4値を
claude-code-proxy バイナリの availability と ChatGPT subscription auth の
availability に基づいて静的判定していた（P1-5 責務境界、Issue #2273
起源）が、この判定ロジック自体（`_spark_capability()` および
env-only probe への配線）は撤去された。

現在の `checks.spark.status` は `not_required`（`spark_mode` 未指定。
ordinary caller は無変更で動作し続ける）または `retired`（`spark_mode`
に `required`/`preferred` いずれかの値が指定された場合。
proxy バイナリ・ChatGPT auth の実 availability に関わらず、常に
deterministic に `retired` となり、`decision: blocked` を返す）の
2値のみを取る。旧 `eligible` / `fallback_only` / `unavailable` の
live 判定・fallback 継続経路は存在しない。

`assess()`（`scripts/claude-gpt/workflow_capability_preflight.py`）の
`spark_mode`/`spark_fallback` キーワード引数自体は、既存の ordinary
caller（`spark_mode=None` で呼ぶもの）との呼び出し契約維持のため
シグネチャとして残っているが、非 `None` 値に対する live 判定分岐は
撤去済みである。
