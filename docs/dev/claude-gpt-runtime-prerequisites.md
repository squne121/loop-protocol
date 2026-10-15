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
日常の launcher には常設しない。permission mode も launcher は自動設定しない。operator が `--` の
後に明示した引数だけをそのまま Claude Code に渡す。

### Claude-GPT Agent View の明示起動

Claude-GPT に限り、対象 Git repository で次のように operator が権限モードを明示して起動する。

```bash
scripts/claude-gpt/launch.sh -- agents --permission-mode bypassPermissions
```

`--` は launcher と Claude Code の引数を分ける。`--permission-mode bypassPermissions` は
launcher の既定値ではなく、その起動で Claude Code に渡す明示的な選択である。Native Claude Code の
通常起動・グローバルな permission default は変更しない。**bypassPermissions は permission prompt
だけでなく Claude Code の安全確認も無効化する。** Claude Code 公式は、被害を封じ込められる隔離
container / VM 内での利用を推奨する。ホスト上の WSL2 は、そのような隔離環境とはみなさない。
repository の hook や policy が引き続き動作する場合もあるが、完全な安全境界にはならない。

Claude Code が求める bypass mode の初回警告（disclaimer）承諾と workspace trust は別々に
人間が判断する。trust は Git repository 一律ではなく workspace / directory ごとの判断であり、
nested repository や別の linked worktree では別途承諾が必要になる場合がある。launcher が承諾・
trust を偽装したり自動化したりしない。

権限モードは GitHub/proxy 認証の代替ではない。Agent View が
別 repository の子へバックエンド接続設定を継承・転送できるかも独立した問題であり、
その実機検証・worktree 互換性の判断は #3052 で行う。fake Claude の argv テストや
`claude agents --json --all` の一覧だけでは、子の実行モード・LLM 応答を実証できない。

### Agent View cross-repo gateway（別 repo の子へ gateway を渡す opt-in 手順、Issue #3055）

親ディレクトリ（`~/projects`）の Agent View から `@repo` で別 Git repository へ dispatch する子は、
**その子の cwd の settings** から gateway を読む。launcher が `exec` 前に export した shell env は、
Claude Code 公式仕様では supervisor が同じ gateway を export した shell から起動され、かつ同一
directory への dispatch / 背景化に限って転送される。別 repo への dispatch では届かないため、
子が `Claude Max` 表示や model access error になり得る（#3052 の実機観測）。公式は gateway 変数を
settings file の `env` block に置き、Agent View に渡した `--settings` が dispatch 先へ引き継がれると
説明している（https://code.claude.com/docs/en/agent-view の「LLM gateway」）。

default launcher は変更しない。新しい wrapper も追加しない。operator が repo 外の絶対 path に
settings file を一度生成し、その path を `--settings` で渡すだけである。env 値は launcher の
canonical 定義（`lib.sh`）を `--dry-run` で取り出して使うため、model alias を二重管理しない。

```bash
# 1. 接続先が loopback の claude-code-proxy であることを --check-only で確認してから、canonical env を
#    repo 外の operator-local settings へ生成する（token は "unused" の placeholder。--check-only が失敗した場合は生成しない）
mkdir -p "$HOME/.claude-gpt"
sh scripts/claude-gpt/launch.sh --check-only >/dev/null 2>&1 \
  && sh scripts/claude-gpt/launch.sh --dry-run 2>/dev/null \
  | uv run --locked python3 -I -c 'import json,sys; print(json.dumps({"env": json.load(sys.stdin)["launch_env"]}, indent=2))' \
  > "$HOME/.claude-gpt/agent-view-settings.json"

# 2. 親ディレクトリで Agent View を開く（--settings は dispatch 先の子へ引き継がれる）
cd ~/projects && claude agents --settings "$HOME/.claude-gpt/agent-view-settings.json"
```

- 生成後は `test -s "$HOME/.claude-gpt/agent-view-settings.json"` で空でないことを確認する（`--check-only` 失敗時は file を作らない）。
- `--settings` には inline JSON ではなく**絶対 path** を使う。inline JSON は process 引数と保存済み job state
  （`respawnFlags`）に残るため避ける。path が存在しない場合 Claude Code は `Settings file not found` で終了する。
- 生成する `env` は launcher が通常起動で export する全 key（gateway・role alias・`CLAUDE_CODE_*` 互換設定・
  `LOOP_TASK_CONTEXT_RUNTIME_VARIANT`・`CLAUDE_GPT_CLAUDE_BIN` の local path）であり、gateway 以外の値も含む。
  `--dry-run` は診断せず ambient の `ANTHROPIC_BASE_URL` をそのまま出力するため、必ず手順 1 のとおり
  `--check-only`（loopback host・userinfo / path なしの URL のみ受理、不正なら exit 7）を先に通す。
- 生成する file の `ANTHROPIC_AUTH_TOKEN` は現行 loopback proxy 用の dummy 値 `unused` だけである。
  実 credential をこの file・Git・Issue/PR に書かない。一般の gateway credential へ流用しない。
- 適用範囲は、その `claude agents` から dispatch した session に限る。`~/.claude/settings.json`・Native Claude Code の
  通常起動・`scripts/claude-gpt/launch.sh` の通常起動は変えない。`--settings` は ambient の user/project 設定より
  優先されるため、proxy の port や role alias を変えたら file を再生成する（古い file は古い route を指す）。
- `--permission-mode bypassPermissions` が必要な場合は、上の「Claude-GPT Agent View の明示起動」の注意と
  承諾（bypass disclaimer・workspace trust）に従い、operator が `claude agents` へ明示的に追加する。
  この手順は permission mode を注入しない。dispatch 先の repo ごとの workspace trust も別途必要である。
- 確認: 子の UI が `Claude Max` ではなく `gpt-6-sol[1m]` を示し、子が実際に応答すること。proxy log との
  一対一対応は取れない場合があり、その場合は provider route を未確認として扱う。
- 戻し方: `claude agents` を `--settings` なしで開き直し、`rm "$HOME/.claude-gpt/agent-view-settings.json"` する。
  Native Claude Code へ戻す場合は `claude` を直接起動する（global 設定は一切変更していない）。
- **必要性は実機の 2 arm 対照で `needed` と判定された（限定つき）**: Issue #3055 の AC1 では、同じ trust 済み fixture・cwd・
  permission mode・最初の prompt で、control（`--settings` なし）が model access error で失敗し、candidate（`--settings env` あり）は
  実 LLM（`gpt-6-sol`）が応答して、evaluator が `needed`（exit 0）を返した（Claude Code 2.1.299。証跡:
  https://github.com/squne121/loop-protocol/issues/3055#issuecomment-6171360693 ）。AC3 では、candidate を再開した後の 2 回目の probe に対する
  実 LLM 応答と、resume 境界が evaluator で観測され、`observed_postcondition: true`（exit 0）となった（再開後は 2.1.300。証跡:
  https://github.com/squne121/loop-protocol/issues/3055#issuecomment-6190229095 ）。ただし制約として、各 arm は 1 試行である。
  UI 操作（dispatch・attach）は OWNER の自己申告で、evaluator は独立に観測できない。proxy request との一対一相関は未確認で、
  provider 固有の route 成功は主張しない。Haiku route と process 再起動・settings 再読込は未検証である。この結果から一般的な必要性までは
  主張しない。`scripts/claude-gpt/tests/agent_view_gateway_live_vc.py` は、条件が変わった環境で同じ判定を再現するために引き続き使える
  （`needed | unnecessary | inconclusive`）。

#### 実機 2 arm 対照の operator 手順（AC1 / AC3）

`claude --bg` や合成 fixture は代用にならない。operator が実際の Agent View UI で、同じ Claude Code version・
proxy・trust 済み fixture（例: owner 所有の disposable repo `~/projects/agentview-3052-zEnHieYt`、clean を確認）・
permission 条件で次の 2 arm を**順に**実行する。#3052 で失敗した条件は「`launch.sh` が shell にだけ gateway を
export した状態で別 repo へ dispatch する」ことなので、**両 arm とも `launch.sh` 経由**で開き、差を `--settings` の有無
だけにする（gateway が全く無い shell を control にすると native fallback が自明に出て、仮説の検証にならない）。
`~/.claude/settings.json` の `defaultMode` も 2 arm で同じにする。シェル変数 `REPO` は本 repository の絶対 path を保持する
（例: `REPO=/home/<user>/projects/LOOP_PROTOCOL`）。launcher は `cd ~/projects` で親ディレクトリへ移動した後でも
絶対 path で呼び、evaluator は後述のとおり `uv run --directory "$REPO"` で repository root を基準に実行する。

1. Control: `cd ~/projects && sh "$REPO/scripts/claude-gpt/launch.sh" -- agents --permission-mode bypassPermissions`（`--settings` なし）。
2. Candidate: Agent View を閉じ、`cd ~/projects && sh "$REPO/scripts/claude-gpt/launch.sh" -- agents --settings "$HOME/.claude-gpt/agent-view-settings.json" --permission-mode bypassPermissions` で開き直す。

evaluator は両 arm の role alias（`providerEnv`）・実効 permission-mode・child cwd・Claude Code version が同一であることを
要求し、満たさなければ `inconclusive` にする。supervisor を 2 arm 間で再起動したかは記録しておく（evaluator は検査できない）。

各 arm で fixture を `@<repo名>` で指定し、次の同一 prompt を dispatch する。prompt に答えを含めないため、応答の echo と
区別できる。

```text
@agentview-3052-zEnHieYt cwd の `git rev-parse --short HEAD` を実行し、`AVGW3055 <出力>` の 1 行だけで答えてください。
```

dispatch 後、子が完了（または失敗）してから job id を取得して evaluator を実行する。job id は 8 桁 hex である。

```bash
claude agents --json --all | jq -r '.[] | select(.cwd == env.HOME + "/projects/agentview-3052-zEnHieYt") | [.id, .state, .startedAt] | @tsv'

export CLAUDE_GPT_AV_UI_DISPATCHED=1   # 上記を UI から実際に dispatch した場合のみ operator が宣言する
export CLAUDE_GPT_AV_TARGET_CWD="$HOME/projects/agentview-3052-zEnHieYt"
export CLAUDE_GPT_AV_EXPECT_TEXT="AVGW3055 $(git -C "$CLAUDE_GPT_AV_TARGET_CWD" rev-parse --short HEAD)"
export CLAUDE_GPT_AV_CONTROL_ID=<control の id>
export CLAUDE_GPT_AV_CANDIDATE_ID=<candidate の id>
REPO=<本 repository の絶対 path>
uv run --directory "$REPO" --locked python3 scripts/claude-gpt/tests/agent_view_gateway_live_vc.py --scenario gateway-control-comparison
uv run --directory "$REPO" --locked python3 scripts/claude-gpt/tests/agent_view_gateway_live_vc.py --scenario dispatched-child-and-resume
```

evaluator の相対 path（`scripts/...` と既定の `artifacts/...`）は `uv --directory` が切り替える repository root を基準に解決される。
`cd ~/projects` した親ディレクトリから `--directory` 無しで実行すると `~/projects/scripts/...` を探して exit 2 になる
（`uv --project` だけでは相対引数の基準 cwd は変わらない）。

AC3 の `dispatched-child-and-resume` は、candidate が最初の prompt に応答して job が終端（`done`）した後に、
pause または supervisor 再起動を経て `Enter` で再開し、同じ prompt を 2 回目の user turn として送って応答させ、
job が再度 `done` になってから実行する。evaluator は `timeline.jsonl` の terminal 行を resume 境界とし、境界より前の
有効な gateway 応答・境界より後の probe に続く有効応答・最新応答が有効・job の最終 state が `done` かつ
`lastTerminalAt` が probe より後であることを要求する。境界を観測できない（timeline 欠落・terminal 無し・後続 turn 無し）
場合は `assertions.resume_boundary` が `unverified` となり exit 1 で、2 turn の応答（`two_turn_response`）とは別に記録する。
resume 境界にできる terminal は `done` / `failed` / `stopped` だけで、入力待ちの `blocked` は process が生存しているため境界にしない。
pre-boundary の有効応答は、最初の probe 以後かつ境界（T1）以前の時刻でなければならず、時刻が無い応答は
境界へ束縛できないため `unverified` とする。
post-boundary の有効応答は、境界後の最初の probe から次の human turn までの区間に必要である。
process の再起動・settings 再読込そのものは観測できないため `process_restart_observed` は常に `unverified` である。

evaluator の判定は次のとおり。`assertions` の `unverified` は PASS ではない。

- `needed`（exit 0）: 同じ要求 model（`--model`）・同じ最初の probe prompt の初回実行区間で、control が model access error か
  gateway 外 model への fallback で失敗し、candidate が gateway model で応答した。
- `unnecessary`（exit 0）: 同条件の初回実行区間で両 arm が gateway model で応答した。docs の本節は不要になる。
- `inconclusive`（exit 1）: arm の条件・version・cwd・要求 model・最初の probe prompt が不一致、初回実行区間に有効応答も
  failure class も無い、candidate の初回が失敗し追加 turn だけ成功した、または失敗が gateway と無関係。
- exit 77: job id・target cwd・UI 宣言が無い（SKIP）。

AC3 の exit 0 は、`--settings` の**内容**（file の場合は中身）が canonical の loopback gateway と全 role alias に一致し、
transcript に実効 `permission-mode` event があって宣言と一致し、main model の実応答があり、`--settings` に実 credential（API key や `unused` 以外の token）が無いことを意味する。
transcript が期待 path に無く別の projects dir から一意に採用された場合は、job state の cwd だけでは成功にせず、transcript 各行の
実 cwd を read-only の `git rev-parse --git-common-dir` で target と照合する。同一 repository（primary checkout または linked worktree）
だけを受理し、別 clone・別 repository・cwd 欠落・候補重複・common-dir 取得不能は `child_cwd_in_target: false` として扱う。
`--settings` の file は評価時に読むため、dispatch 後に再生成した file は区別できない。Haiku route は
実際に `gpt-6-luna` の応答が transcript に現れた場合だけ `observed` で、alias の設定値だけでは `unverified` のままである。
UI からの dispatch は operator の自己申告でありスクリプトは独立に観測できない。proxy request との一対一相関も取得できない
場合は `unverified` とし、provider route は主張しない。

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
