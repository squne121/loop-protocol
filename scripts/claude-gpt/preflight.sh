#!/bin/sh
# scripts/claude-gpt/preflight.sh
#
# claude-gpt の起動前 preflight 入口。単体実行可能。
#
# Issue #2925 で launcher が upstream Minimal client contract へ縮退したため、この script は
# 2 つの役割だけを持つ（旧 launcher の isolated HOME / settings / credential / path 検査は
# 検査対象そのものが無くなったため撤去した）。
#
#   1. （引数なし / --env-only）接続先 server の診断。`launch.sh --check-only` と同一の
#      実装・同一の JSON を返す。ChatGPT subscription 認証は proxy server の所有者の責務
#      であり、この script は認証状態を判定しない。到達性と /v1/models の required model
#      set（gpt-6-sol および gpt-6-luna）だけを接続先 server に対して確認する。
#   2. （--workflow-profile <profile>）`workflow_capability_preflight.py` への薄い dispatcher。
#      `CLAUDE_GPT_WORKFLOW_CAPABILITIES_V1` JSON を返す（判定ロジックはこの script に
#      複製しない）。
#
# Exit code:
#   0   接続先 server の診断 PASS
#   7   接続先 server の診断失敗（到達不能 / base URL 不正 / required model 不足）。
#       呼び出し元は「実行環境が利用不能」として SKIP（exit 77）へ変換してよい。
#   --workflow-profile 時は workflow_capability_preflight.py の exit code をそのまま返す。

SELF_PATH=$0
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$SELF_PATH")" && pwd -P)

if [ "${1:-}" = "--workflow-profile" ]; then
  shift
  # `workflow_capability_preflight.py` は trusted `uv` の可用性を自身で判定する
  # （`trusted_runtime_capabilities.check_trusted_uv`）。未検証の PATH `uv` を先に
  # 実行しないよう、system `python3` から直接起動する。
  python3 "$SCRIPT_DIR/workflow_capability_preflight.py" --profile "$@"
  exit "$?"
fi

case "${1:-}" in
  "" | --env-only)
    exec "$SCRIPT_DIR/launch.sh" --check-only
    ;;
  *)
    printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"blocked","reason":"unknown_preflight_option","option":"%s"}\n' "$1" >&2
    exit 2
    ;;
esac
