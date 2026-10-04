# Claude-GPT ランタイム前提条件

このドキュメントは、Claude-GPT の default launcher（`scripts/claude-gpt/launch.sh`）の
Minimal client contract（Issue #2925）と、
`scripts/claude-gpt/preflight.sh --workflow-profile issue-to-impl`
（実装は `scripts/claude-gpt/workflow_capability_preflight.py`、構造化結果は
`CLAUDE_GPT_WORKFLOW_CAPABILITIES_V1`）が判定するランタイム前提条件のうち、
恒久的な参照先として残すべき運用メモをまとめる（Issue #2273）。

## 薄い wrapper としての Minimal client contract（default launcher の契約、Issue #2925）

`scripts/claude-gpt/launch.sh` は、既に起動している loopback `claude-code-proxy` へ Claude Code を
向けるために必要な process env だけを追加して `claude` を `exec` する薄い wrapper である。
upstream の案内（https://claude-code-proxy.raine.dev/using/configure-claude-code/ と
https://claude-code-proxy.raine.dev/using/for-coding-agents/）を source of truth とし、
proxy server の設定（`CCP_*`、`PORT`、`config.json`）と Claude Code client の設定
（`ANTHROPIC_*`、`CLAUDE_CODE_*`）を分離する。

### launcher が追加する env（これで全て）

Minimal client contract（upstream の案内どおり）:

- `ANTHROPIC_BASE_URL`: 接続先 loopback proxy。未設定なら upstream 既定の `http://127.0.0.1:18765`
- `ANTHROPIC_AUTH_TOKEN`: `unused`（client の credential 要件を満たすだけの dummy 値）
- `ANTHROPIC_MODEL`: `gpt-6-sol[1m]`（main request の model）
- `ANTHROPIC_SMALL_FAST_MODEL`: `gpt-6-luna[1m]`（title 生成など小さい request の model）
- `CLAUDE_CODE_AUTO_COMPACT_WINDOW`: `272000`（ChatGPT backend の実 context 上限に合わせた compaction window）
- `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`: `1`（不要な background traffic の抑制）
- `CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK`: `1`（部分完了した stream の non-streaming 再試行を禁止）

現行 contract として残す設定（isolation の補償処理ではない）:

- role-based model routing（現行値を維持）: `ANTHROPIC_DEFAULT_OPUS_MODEL` と `ANTHROPIC_DEFAULT_SONNET_MODEL` は
  `gpt-6-sol[1m]`、`ANTHROPIC_DEFAULT_HAIKU_MODEL` は `gpt-6-luna[1m]`
- Auto mode 互換設定: `CLAUDE_CODE_AUTO_MODE_SERVER=0`（classifier request を client 側から proxy 経由で routing する）

非 behavior-changing な runtime identification（既存 consumer 向けの 2 個）:

- `LOOP_TASK_CONTEXT_RUNTIME_VARIANT=claude_gpt`: Task Context が runtime flavor を識別するためだけに使う
- `CLAUDE_GPT_CLAUDE_BIN=<解決済みの claude 実行ファイル path>`: session manifest hook の `resolveRuntimeLane` が
  この変数の有無だけから `runtime_lane: claude_gpt` を識別する。旧 launcher からの既存 carrier

`gpt-6-sol[1m]` の `[1m]` は Claude Code 側の local context policy hint であり、proxy は upstream へ
転送する前に除去する。role alias の配分変更（例: Sonnet 系 SubAgent を `gpt-5.6-terra` へ移す）と
`CLAUDE_CODE_AUTO_MODE_SERVER` の廃止は、launcher 縮退とは別の explicit specification change として
別 Issue で扱う。`CCP_AUTO_REVIEW_MODEL` は proxy **server** 側の設定であり、launcher は設定しない
（upstream 既定の Codex では未設定時に `gpt-6-luna` へ routing される）。

### default path から撤去した層

旧 launcher の履歴は Git history が rollback authority であり、legacy-full profile を fallback として
併存させない。

- `HOME` / `XDG_*` / `CLAUDE_CONFIG_DIR` の隔離（ambient な Native user/config surface を共有する）
- launcher 生成の `--settings` / `settings.local.json`、strict MCP の空 config、`--permission-mode auto` の強制注入、
  `--agents` 注入、custom `autoMode` prose、`enabledPlugins` 無効化
- isolation の穴埋めだった GitHub / AGY credential・path carrier、Task Context state-root carrier、
  native settings path の re-entrant handoff、native session registry の symlink bridge
- launcher 固有の hook / settings injection（Latitude Stop hook、`PermissionRequest` hook、Spark 退役 hook、
  smoke 用 `CLAUDE_GPT_RUNTIME_SMOKE_HOOKS` の hook sink）と `BUN_OPTIONS` の unset
- proxy の起動・停止・再起動（port 割当、`env -i` 起動、loopback bind 確認、`CCP_CODEX_TRANSPORT` 注入）
- launcher 起動に紐づく test-only wiring（`live_issue_create_canary.sh`、`latitude_hook.py`、smoke 用の
  canary SubAgent fixture と `--agents` 注入）

`scripts/claude-gpt/auto_mode_canary.py` は削除していない。Allowed Paths 外の consumer
（`.claude/agents/tests/test_issue_editor_runtime_smoke.py` が import する）が残っているためである。
同 canary の launcher-owned `autoMode` / broker 系 mode は撤去した層を前提としており陳腐化している。
整理は consumer を含む follow-up で扱う（blind delete しない）。

通常の child process への環境継承（`GH_CONFIG_DIR` 等の GitHub 認証 env、`SSH_AUTH_SOCK` 等）は
撤去対象ではなく、Native Claude Code と同じく ambient な値がそのまま届く。smoke 用の観測 hook と
`--append-system-prompt-file` は smoke 実行側（`worktree-agent-runtime-smoke`）の opt-in であり、
日常の launcher には常設しない。permission bypass（`--dangerously-skip-permissions`、
`--allow-dangerously-skip-permissions`、`--permission-mode bypassPermissions`）は launcher 経由では
引き続き拒否する。

### 接続先 server の診断

診断の authority は、`ANTHROPIC_BASE_URL` が実際に接続する running server である。

```bash
bash scripts/claude-gpt/launch.sh --check-only
```

- 到達性と `/v1/models` の required model set（`gpt-6-sol` **および** `gpt-6-luna`）を同じ URL に対して
  判定する。一方だけでは PASS しない。不足は推論能力や entitlement の failure ではなく、
  「接続先 server の model catalog 不整合」として分類する。
- 結果は `connected_server`（base URL・到達性・`/v1/models` の HTTP status・不足 model・分類）と
  `local_proxy_binary_auxiliary`（PATH 上の `claude-code-proxy` の path / version）を別項目で返す。
  PATH 上の binary は接続先 server が動かしている binary とは限らないため、補助 evidence に過ぎない。
- server の version は公開 endpoint から取得できない（`/healthz` は `{"ok":true}` のみで version を返さない）
  ため、常に `未確認` と記録する。binary の version で代用しない。
- host は loopback（`127.0.0.1` / `localhost` / `::1`）のみ受け付ける。
- proxy が無い場合は bounded な診断と起動方法（例: `claude-code-proxy serve --port 18765`）を返して
  exit 7 で停止する。launcher は proxy を起動せず、停止もしない。launcher が起動していない shared /
  running proxy は、launcher の終了時にも停止されない。
- `scripts/claude-gpt/repair_proxy.sh` が修復するのは **binary**（`$CLAUDE_GPT_HOME/bin` 配下）であり、
  running server ではない。修復した binary で server を再起動するのは server の所有者である
  （結果 JSON は `repaired_scope: binary_only` / `server_restart_required: true` を返す）。

### 検証の境界

- merge 前: `scripts/claude-gpt/tests/test_minimal_default_contract.py` と
  `test_connected_proxy_diagnostics.py`（実 `launch.sh` を subprocess で駆動する hermetic test）と、
  実 Claude Code process を使う live 検証（`runtime_smoke_test.sh --scenario auto_classifier`、
  `test_live_claude_gpt_named_subagent_resume`、`test_runtime_smoke_issue_to_impl_live.py`）。
- merge 後: 5 件以上の実作業 trial（AC7）。Issue close 条件であり、merge 条件ではない。

### upstream 差分の確認記録（2026-10-04 時点）

実装開始時に再確認した結果（固定知識にしない）。claude-code-proxy の latest release は v0.1.43
（2026-09-30）で、v0.1.42 との差分は `gpt-6.1-sol` の追加のみ。`model_allowlist.rs` の既定 alias
（`haiku` → `gpt-6-luna`、`sonnet` → `gpt-5.6-terra`、`opus` → `gpt-6-sol`）は不変で、現行 launcher の
Sonnet 解決先（`gpt-6-sol`）とは異なる。手元の binary は v0.1.42、Claude Code は 2.1.289 である。

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
- pytest ラッパ（`test_live_native_named_subagent_resume` / `test_live_claude_gpt_named_subagent_resume`）は
  既存の `claude_live` marker 付きで、通常の `pytest` では default `addopts` により deselect される。
  実 live 実行は `uv run --locked pytest -m claude_live <node id>` で明示的に opt-in する（`CI` 環境変数の
  偽装は不要）。
- Claude-GPT adapter も Native adapter と同じ固定 `--settings` overlay を runner が invocation 単位で渡す
  （Issue #2925 以降、launcher は smoke 専用の hook channel を持たない）。scenario overlay は
  `SendMessage` の deny だけを外し、`ListAgents` deny と `crossSessionInbound: refuse` を維持する。
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
