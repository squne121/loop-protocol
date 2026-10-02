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

## Named SubAgent resume scenario（名前指定 SubAgent の resume 検証、Issue #2840）

repository-owned の通常 launcher（`scripts/claude-gpt/launch.sh`）で、ordinary named SubAgent を
spawn して complete させ、`SendMessage(to=<name>)` で同じ agent ID のまま resume して再度 complete
させる能力を、fresh session から再実行できるようにした手順である。正本は
`.claude/skills/worktree-agent-runtime-smoke/SKILL.md` の同名見出し（Named SubAgent resume
scenario）であり、ここには再利用に必要な要点だけを置く。

- 固定の公開試験 prompt:
  `.claude/skills/worktree-agent-runtime-smoke/fixtures/named-subagent-resume.prompt.md`
- 互換性説明（`--append-system-prompt-file` で当該 invocation にだけ適用する。恒久的な
  system-prompt 注入ではなく、launcher にも常設しない）:
  `.claude/skills/worktree-agent-runtime-smoke/fixtures/named-subagent-resume.compat.md`
- 補足なしで backend model が常に自発的に name を付けることは合格条件にしない。互換性説明の有無による
  結果は観測として記録する。

### 直接実行する

検証対象の linked worktree で次を実行する（`--claude-bin` を省略すると、その checkout の
`scripts/claude-gpt/launch.sh` が絶対 path に解決される。Native の control は
`--claude-adapter native`）。

```bash
mkdir -p artifacts
uv run --locked python3 scripts/agent-ops/run_worktree_agent_runtime_smoke.py \
  --runtime claude --mode structured --claude-adapter claude-gpt \
  --worktree "$(pwd)" \
  --prompt-file "$(pwd)/.claude/skills/worktree-agent-runtime-smoke/fixtures/named-subagent-resume.prompt.md" \
  --append-system-prompt-file "$(pwd)/.claude/skills/worktree-agent-runtime-smoke/fixtures/named-subagent-resume.compat.md" \
  --named-subagent-resume \
  --named-resume-evidence-json "artifacts/runtime-verification-2840-claude-gpt-$(git rev-parse --short=8 HEAD).json" \
  --output-dir "$(mktemp -u)" --timeout-seconds 540
```

- exit `0` は因果鎖が全て成立（evidence の `verdict=pass`）、`1` は失敗、`77` は SKIP である。SKIP は
  PASS ではない。causal evidence が観測できない場合（例: #2846 の `no_evidence`）も 77 とし、
  fixture・static schema・別 backend・agent ID lane への fallback による成功は FAIL として扱う。
- Claude-GPT adapter は caller `--settings` を受け付けない。launcher 固定値
  `CLAUDE_GPT_RUNTIME_SMOKE_HOOKS=subagent-name-resume` が `SendMessage` の deny だけを外し、
  `ListAgents` deny と `crossSessionInbound: refuse` を維持する。既存の固定値
  （`subagent-start-stop` / `hook-sink-multi-turn`）の出力は変更しない。
- 失敗時の原因層は evidence の `failure_layer` に出る（`client_schema` / `launcher_config` /
  `proxy_translation` / `backend_model_emission` / `hook_lifecycle` / `unclassified`）。
  `unclassified` は PASS にならない。

### Task Context の判定を呼び出す

runner は Task Context の semantic verdict を持たない。Task Context 固有の判定は、既存の
`scripts/task-context/task_context_runtime_smoke_verifier.py` の `orchestrate_runtime_smoke()` が
所有する。同関数の `runner_argv_extra` に `--named-subagent-resume`、`--append-system-prompt-file
<互換性説明の絶対 path>`、`--named-resume-evidence-json <path>` を渡し、`prompt_file` に上記の固定試験
prompt を指定して呼び出す。具体的な呼び出し例は `worktree-agent-runtime-smoke` Skill の同名見出しを
参照する。

### evidence の再利用

evidence は tested HEAD・Claude Code version・proxy version・model route・launcher hash・fixture と
互換性説明の content sha256 を記録する。再利用可否は runner の `evaluate_evidence_freshness(recorded,
current)` で決める。`scripts/claude-gpt/` の変更は Claude-GPT canary だけ、runner・fixture・hook と
effective settings 登録（`.claude/settings.json`、`.claude/hooks/task_context/`、
`scripts/task-context/`）の変更は両 adapter の canary を取り直す。それ以外の無関係な commit だけを
live canary の再実行理由にしない。
