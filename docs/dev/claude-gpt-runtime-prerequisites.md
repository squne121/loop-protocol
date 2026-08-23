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

## Spark delegation route の判定範囲（P1-5 責務境界）

`checks.spark.status`（`not_required` / `eligible` / `fallback_only` /
`unavailable`）は、claude-code-proxy バイナリの availability と ChatGPT
subscription auth の availability のみに基づく静的判定である。すなわち、
この preflight チェックが確認しているのは「Spark 委譲経路を構成する
proxy バイナリと認証情報が揃っているか」という起動可否の観点のみであり、
それ以上の意味を持たない。

**`spark.status: eligible` は、実行時に実際に使われるモデルが
`CLAUDE_CODE_SUBAGENT_MODEL` の override 意図と適合していることの証明では
ない。** 言い換えると、Spark delegation route が利用可能だと preflight が
判定したとしても、実際に起動される Agent が意図したモデルに正しく
束縛されている保証はこの preflight の範囲外である。実効 model 適合性の
検証は、PR #2285 / Issue #2274 で導入された Agent 起動直前の model gate
（`resolvedModel` ベースの判定）の責務であり、本 preflight はその責務を
重複実装しない。両者は役割が異なる別レイヤーの安全機構として、
意図的に分離されている。
