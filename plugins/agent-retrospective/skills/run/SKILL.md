---
name: run
description: Claude Code / Claude-GPT の run 証跡・repository・Web から改善候補（agent_improvement_candidate/v1）を proposal-only で生成する継続的 retrospective の orchestrator（agent-retrospective plugin distribution 版）。人間の明示トリガーでのみ起動する（自動起動しない）。`.claude/` を持たない任意の repository に `claude --plugin-dir` でインストールして使う portable 版。
disable-model-invocation: true
allowed-tools: Bash
---

# Agent Retrospective (Plugin distribution, Orchestrator)

`agent-retrospective` plugin は、repository / runtime / Web の 3 source から収集した evidence を、
独立 2 段の SubAgent（observer wave → evaluator）で解釈し、`agent_improvement_candidate/v1`
準拠の改善候補を **proposal-only** で生成する portable Claude Code plugin である。

これは `.claude/skills/agent-retrospective/`（project Skill 版）の distribution 版であり、
project Skill 本体はこの plugin では変更しない（Issue #2240 Out of Scope）。plugin 版は
project Skill 版が前提とする AGY role adapter（`gemini-cli-headless-delegation`）、
Latitude CLI enrichment、Claude-GPT `transport_log.py` へ一切依存しない、軽量な独立実装
（`codebase-investigator`/`web-researcher` は Read/Grep/Glob・native WebSearch/WebFetch のみ）
として再設計されている。

## Orchestration owner（実行主体）

```yaml
orchestration_owner: root Skill / main conversation
invocation_transport: single Bash call to the stable executable entrypoint
run_retrospective.py:
  role: stable executable entrypoint (CLI) that owns the whole call graph
  phases: [prepare, validate-observers, prepare-evaluator, finalize]
  invokes_observers_and_evaluator_via: headless CLI subprocess (`claude --plugin-dir <plugin_root> -p --agent agent-retrospective:<name>`)
agent_tool_calls:
  interactive_agent_tool: never used by this Skill
  prohibited_owner: observer/evaluator leaf agents (never spawn nested SubAgents)
```

root Skill は本 SKILL.md の手順に従い **単一の Bash 呼び出しで `scripts/run_retrospective.py` を実行する**
のみを行う。`run_retrospective.py` 自身は、その 1 プロセス内で collector closure・observer manifest
構築・observer/evaluator 呼び出し（headless CLI subprocess `claude --plugin-dir <plugin_root> -p
--agent agent-retrospective:<name>`）・fan-in・evaluator 呼び出し・`finalize` を一続きで実行し、
最終的な proposal-only `PublishRequest`（または typed failure）を stdout に出力する。**Claude Code
の interactive `Agent` tool は本 Skill のどの経路でも一切使わない**。

## Procedure（手順）

### 1. 実行

```bash
UV_PROJECT_ENVIRONMENT="${CLAUDE_PLUGIN_DATA}/venv" \
uv run --project "${CLAUDE_PLUGIN_ROOT}" --locked python3 \
  "${CLAUDE_PLUGIN_ROOT}/skills/run/scripts/run_retrospective.py" \
  --repo-root "${CLAUDE_PROJECT_DIR}" \
  --plugin-root "${CLAUDE_PLUGIN_ROOT}" \
  --state-backend fixture \
  --task "$ARGUMENTS"
```

`$ARGUMENTS`（Claude Code Skill の呼び出し引数。`/agent-retrospective:run <task text>` のように
渡す）を `--task` へ機械的に転送する（Issue #2240 fix_delta P0-1(a)/(b)）。`$ARGUMENTS` が空の場合
（引数なしで起動した場合）、`run_retrospective.py` 側が `DEFAULT_TASK`（"find implementation
improvement candidates in the current working tree"）へ自動フォールバックする -- **どちらの経路でも
observer には実質のある task が必ず渡り、`findings: []` を決め打ちする pre-fix のデフォルトプロンプト
には到達しない**（Issue #2240 fix_delta P0-1、旧実装は `prompts=None` のとき 3 observer 全員に
「findings を空配列にせよ」という空調査プロンプトを渡していた回帰バグ）。

`--repository-id` / `--target-issue` / `--request-id` / `--idempotency-key` はすべて任意である
（下記「入力契約の自動導出」参照）。`--schema-dir` は observer/evaluator 用 JSON Schema の配置先を
差し替える場合にのみ指定する override option であり、通常は省略してよい（既定値は
`${CLAUDE_PLUGIN_ROOT}/skills/run/schemas`）。`--runtime-evidence-file <path>` は明示的に runtime
evidence を渡したい場合にのみ指定する任意 option（下記「observer wave」参照。省略時（標準経路）は
`retrospective-runtime-observer` 自体を起動しない）。

`uv run --project "${CLAUDE_PLUGIN_ROOT}" --locked` は、`${CLAUDE_PLUGIN_ROOT}/pyproject.toml` /
`uv.lock` が固定する Python dependency closure（`jsonschema`。test-only の `pytest` は
`skills/run/scripts/tests/` の offline regression test 専用で、`--group test` を明示しない
標準の `--locked` 実行時には同期されない）を使って `run_retrospective.py` を起動する。`--project` は project root
（依存解決対象）のみを変更し、cwd は解析対象 `${CLAUDE_PROJECT_DIR}` のまま維持される。
`UV_PROJECT_ENVIRONMENT="${CLAUDE_PLUGIN_DATA}/venv"` は `uv run --project` が既定で作成する
`.venv` の設置先を明示的に `${CLAUDE_PLUGIN_DATA}` 配下へ退避する（Issue #2240 fix_delta P1-3。
plugin root は Claude Code のアップデートで置き換え可能な transient 領域であり、`${CLAUDE_PLUGIN_
DATA}` こそが永続データの正しい置き場という公式仕様に合わせる）。

root Skill が用意するのはこの 1 回の Bash 呼び出しのみ。内部で `run_retrospective.run_cli()` が
以下を順に実行する（各関数の詳細は `scripts/run_retrospective.py` 本体の docstring を正本とする）。

### 2. 入力契約の自動導出

- `--repository-id` を省略した場合、`git remote get-url origin` から `owner/repo` を自動導出する
  （`default_repository_id_from_git_remote()`）。導出できない場合は明示指定が必要（`repository_id_
  unresolved` で typed failure を返す）。
- `--request-id` / `--idempotency-key` を省略した場合、実行ごとに UUID を自動生成する。
- `--target-issue` を省略した場合、issue-less run として扱う。`PublishRequest.target_issue` は
  `null` になる（架空の issue 番号を捏造しない）。

### 3. 準備（prepare）

run-scoped temp dir（mode `0700`）確保 → `build_repository_collector()`（repository source
collector を `base_sha` 単一引数の closure へ束縛）を含む collectors を `prepare()` に渡し、
`run_id`（run-scoped nonce）と `base_sha`（一度だけ解決、以降再解決しない）を固定した
`RunContext` と `SourcePlan` を得る。

`base_sha` の解決は `git rev-parse <base_ref>`（既定 `--base-ref HEAD`）で行う。project Skill 版の
`_base_sha_resolver()` が `git rev-parse main` を決め打ちする実装はこの plugin 版には持ち込まない
-- `HEAD` を snapshot authority として使うか、呼び出し側が明示 `--base-ref` を指定できる（Issue
#2240 AC5、default branch 名を `main` に固定しない）。

### 4. observer wave（fan-out、`active_observer_manifest()` -- 標準経路は 2 件、runtime evidence
明示指定時のみ 3 件）

以下 3 observer のうち **アクティブな manifest**（`retrospective-runtime-observer` は
`--runtime-evidence-file` が明示指定された場合のみ含める。Issue #2240 fix_delta P0-1(d)。
標準経路は runtime evidence を埋め込む手段を持たないため、常に空調査になる呼び出しを起動して
model call を浪費しない）の `AgentInvocationRequest` を組み立て、`invoke_agent()`（headless CLI
subprocess `claude --plugin-dir <plugin_root> -p --agent agent-retrospective:<name>
--output-format json --json-schema <schema 本文> --allowedTools <観測者ごとの最小 tool 集合>
--no-session-persistence`、prompt は stdin 経由）で **並列に**（Issue #2240 fix_delta P0-1(e)。
`concurrent.futures.ThreadPoolExecutor`。fail-closed 判定自体は `observer_requests` の安定順序で
行うため、実際の完了順に依存しない）呼び出す:

- `retrospective-runtime-observer`（interpreter role。Claude Code/Claude-GPT session evidence の
  解釈専用。project Skill 版と同一契約。呼び出し元がプロンプトへ直接埋め込んだ evidence のみを
  解釈する leaf。`--allowedTools` なし〈frontmatter `tools: []` と一致〉。`--runtime-evidence-file`
  が明示指定された場合のみ manifest に含まれる）
- `codebase-investigator`（advisory role。**plugin 版は Read/Grep/Glob のみを使う軽量な独立実装**
  であり、project Skill 版が必須化する AGY delegation〈`gemini-cli-headless-delegation`〉には
  一切依存しない。`--allowedTools Glob Grep Read`）
- `web-researcher`（discovery role。**plugin 版は native WebSearch/WebFetch のみを使う軽量な独立
  実装** であり、AGY grounded research には依存しない。`--allowedTools WebFetch WebSearch`。この
  plugin には独立した Web collector の再取得実装は存在しない -- web evidence の真正性は、evaluator
  が返す evidence_ref の `resource_identity`（citation URL）を `run_retrospective.py` 側で
  `web-researcher` 自身が実際に返した `citation_url` と突合するクロスチェックのみで担保する
  〈Issue #2240 fix_delta P0-2(e)。「Web collector の再取得済み digest と一致しない場合は
  reject」という旧記述は、実装されていない機構を指す誤った説明だったため削除した〉）

`--agent` フラグには常に plugin-scoped identifier（`agent-retrospective:<name>`。bare name は
使わない）を渡す。`--plugin-dir <plugin_root>` はセッション duration 限定で自動継承されないため、
`run_retrospective.py` が新規 spawn する全ての `claude -p --agent <name>` subprocess の argv に
明示的に再指定する（`AgentInvocationRequest.plugin_root` -> `build_agent_invocation_argv()`）。

`run_observer_wave()` が `EvidenceBundle`（`OBSERVER_RESULT_V1`）へ strict validation し、
`ctx.base_sha` との一致・observer_id 重複なし・アクティブ manifest 完全一致を検証する。失敗時は
`schema_repair_retries: 1` まで repair を試み、それでも失敗すれば **evaluator を起動せず**
fail-closed で終了する。

`DelegatedAgentPermissionPolicy`（`run_retrospective.py`）が実際の subprocess argv
（`--disallowedTools`/`--allowedTools`）と subprocess env（mutation-risk-only な credential
のみを除去し、それ以外〈Claude/Anthropic 公式 auth・provider routing・proxy 変数を含む〉は
親環境から継承する deny-list 方式。Issue #2240 fix_delta P1-2）へ直接反映され、`git commit`/
`git push`/`gh issue`/`gh pr`/filesystem write/unapproved Bash/対象 run 外 resume を拒否する。

### 5. prepare-evaluator（評価準備・fan-in）

全 observer が成功した場合のみ（`partial_agent_output: reject`）、`build_finding_sets()`
（observer role から `finding_authority` を導出）→ `prepare_evaluator_request()` で
`EvaluatorRequest`（schema-controlled projection のみ、raw evidence を含まない）を組み立てる。

### 6. evaluator 起動（observer wave 完了後にのみ、observer と同時起動しない）

fresh context で `retrospective-evaluator`（`agent-retrospective:retrospective-evaluator` として
起動）を headless CLI subprocess で 1 回起動し、`run_evaluation()` で `Evaluation`
（`EVALUATION_RESULT_V1`）を strict validation する。`candidate_records` は
`agent_improvement_candidate/v1` の canonical schema を満たさない限り reject される。
`evaluator_retries: 0`（再試行しない）。

### 7. delta 算出

evaluator 起動直後に `previous_state_provider`（既定 `--state-backend fixture` の空
`FixturePreviousStateProvider`）の `get()` を呼び、`available`/`no_history`/`legacy_unavailable`/
`partial`/`stale` の 5 状態から `compute_delta()` で `new`/`resolved`/`recurrent`/`regressed`/
`unchanged` を算出し、結果を `finalize(..., delta_results=...)` 経由で
`PublishRequest.delta_results` に格納する。

### 8. 確定処理（finalize）

`finalize()` で proposal-only `PublishRequest`（`PUBLISH_REQUEST_V1`）を生成する。
`authorized`/`authorized_by_human`/`authorization_token`/`mutation_capability` はスキーマレベルで
禁止フィールドであり、`PublishRequest` dataclass には存在しない。**この plugin は
Issue/PR/repository への実際の mutation を一切行わない**（proposal-only、`docs/adr/
0007-agent-retrospective-boundaries.md` の mutation boundary を継承）。人間承認・実際の Issue
mutation・run の永続化（Issue comment への投稿）は本 plugin のスコープ外（project Skill 版
`.claude/skills/agent-retrospective/scripts/persist_retrospective_run.py`、Child 5 の責務）。

## Reused Agents（再利用エージェント・capability matrix）

| Role | Authority | 実装 |
|---|---|---|
| runtime observer | interpreter（private runtime evidence + digest）。`--runtime-evidence-file` 明示指定時のみ起動（Issue #2240 fix_delta P0-1(d)） | `agents/retrospective-runtime-observer.md`（project Skill 版と同一契約、frontmatter のみ plugin 制約に合わせて調整） |
| codebase investigator | advisory | `agents/codebase-investigator.md`（**plugin 版軽量独立実装**。Read/Grep/Glob のみ） |
| web researcher | URL discovery / claim interpretation | `agents/web-researcher.md`（**plugin 版軽量独立実装**。native WebSearch/WebFetch のみ） |
| evaluator | privileged synthesis（validated projection のみ） | `agents/retrospective-evaluator.md`（project Skill 版と同一契約、frontmatter のみ plugin 制約に合わせて調整） |

## 実行 budget（要約）

```yaml
observer_parallelism: concurrent  # ThreadPoolExecutor; standard path launches 2 (runtime observer excluded unless --runtime-evidence-file is given -- Issue #2240 fix_delta P0-1(d)/(e))
schema_repair_retries: 1
evaluator_retries: 0
partial_agent_output: reject
timeout_status: typed operational failure
interruption_status: aborted
cleanup_required: true
```

## Ephemeral wire contract（一時的な通信契約）

`SOURCE_PLAN_V1` / `OBSERVER_RESULT_V1` / `FINDING_SET_V1` / `EVALUATOR_REQUEST_V1` /
`EVALUATION_RESULT_V1` / `PUBLISH_REQUEST_V1` の 6 envelope。すべて `schema_version`/`run_id`/
`base_sha`/`source_set_digest` 必須、未知フィールド拒否、oversize 拒否、schema repair retry 上限 1。
2 つの Agent 契約 schema（`schemas/observer_result_v1.schema.json` /
`schemas/evaluation_result_v1.schema.json`）と、canonical 改善候補 schema
（`schemas/agent_improvement_candidate_v1.schema.json`）はこの plugin にバンドルされている。

## Guardrails（ガードレール）

- **Allowed Paths 外を編集しない**
- `run_retrospective.py` は GitHub/Issue へのいかなる mutation も実行しない（proposal-only）
- observer/evaluator は leaf SubAgent（`tools` に `Agent`/`Skill` を含まない、nested delegation 禁止）
- evaluator は observer wave 完了・validated projection 受領前には起動しない
- raw evidence（stdout/stderr/絶対パス/credential）は `evidence_ref` 以外の形で wire envelope を通過しない
- plugin-shipped Agent frontmatter は `hooks`/`mcpServers`/`permissionMode` を含まない（`README.md`
  「Plugin Agent frontmatter の未対応フィールド」節を参照）

## Related（関連情報）

- `scripts/run_retrospective.py`（本 orchestrator の stable executable entrypoint）
- `scripts/collect_snapshot.py`（repository source collector）
- `scripts/validate_retrospective_schema.py`（`agent_improvement_candidate/v1` validator）
- `scripts/clean_install_smoke.sh`（Issue #2240 AC5 の clean install smoke）
- `../../README.md`（plugin overview、unsupported agent frontmatter fields の説明）
- `.claude/skills/agent-retrospective/SKILL.md`（project Skill 版。本 plugin の元になった実装。
  本 Issue ではこのファイル自体を変更しない）
- `docs/adr/0007-agent-retrospective-boundaries.md`（mutation boundary の正本）

## 出力制約 (OUTPUT_BUDGET_V1)

routing-critical な機械可読フィールドは削らず、人間向け説明・証跡・diff 再掲のみを削減する。
