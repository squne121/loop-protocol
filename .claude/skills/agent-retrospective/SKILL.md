---
name: agent-retrospective
description: Claude Code / Claude-GPT の run 証跡から改善候補（agent_improvement_candidate/v1）を proposal-only で生成する継続的 retrospective の orchestrator。人間の明示トリガーでのみ起動する（自動起動しない）。
disable-model-invocation: true
---

# Agent Retrospective (Orchestrator)

`.claude/skills/agent-retrospective/` は、Claude Code / Claude-GPT のセッション証跡・repository・GitHub・Web
の 5 source から収集した evidence を、独立 2 段の SubAgent（observer wave → evaluator）で解釈し、
`agent_improvement_candidate/v1`（#2288 で delta-evaluation 拡張済み）準拠の改善候補を **proposal-only** で
生成する。GitHub Issue への実際の投稿・mutation は `persist_retrospective_run.py`（#2238）が担う。

## Orchestration owner（実行主体）

```yaml
orchestration_owner: root Skill / main conversation
invocation_transport: single Bash call to the stable executable entrypoint
run_retrospective.py:
  role: stable executable entrypoint (CLI) that owns the whole call graph
  phases: [prepare, validate-observers, prepare-evaluator, finalize]
  invokes_observers_and_evaluator_via: headless CLI subprocess (`claude -p --agent <name>`)
agent_tool_calls:
  interactive_agent_tool: never used by this Skill
  prohibited_owner: observer/evaluator leaf agents (never spawn nested SubAgents)
```

root Skill は本 SKILL.md の手順に従い **単一の Bash 呼び出しで `scripts/run_retrospective.py` を実行する**
のみを行う（トリガー判断・停止判断の所有）。`run_retrospective.py` 自身は、その 1 プロセス内で
collector closure・observer manifest 構築・observer/evaluator 呼び出し（headless CLI subprocess
`claude -p --agent <name>`）・fan-in・evaluator 呼び出し・`finalize` を一続きで実行し、最終的な
proposal-only `PublishRequest`（または typed failure）を stdout に出力する。**Claude Code の
interactive `Agent` tool は本 Skill のどの経路でも一切使わない** -- headless CLI subprocess は対話的
`Agent` tool とは別の非対話的呼び出し経路であり、observer/evaluator 側の leaf 制約（`tools` から
`Agent`/`Skill` を除外）とは独立に、root Skill 側の呼び出し方式として一本化されている。詳細な設計判断・
budget の根拠は `references/design-rationale.md` を参照。

## Procedure（手順）

### 1. 実行

```bash
uv run --locked python3 .claude/skills/agent-retrospective/scripts/run_retrospective.py \
  --repository-id squne121/loop-protocol \
  --target-issue <issue-number> \
  --request-id <request-id> \
  --idempotency-key <idempotency-key> \
  --prompts-file <observer_id -> prompt テキストの JSON ファイル>
```

`--schema-dir` は observer/evaluator 用 JSON Schema の配置先を差し替える場合にのみ指定する
override option であり、通常は省略してよい。省略時の既定値は scripts/schemas（`run_retrospective.py` の
`_SCRIPTS_DIR / "schemas"`）。

root Skill が用意するのはこの 1 回の Bash 呼び出しのみ。内部で `run_retrospective.run_cli()` が
以下を順に実行する（各関数の詳細は `run_retrospective.py` 本体の docstring を正本とする）。

### 2. 準備（prepare）

`manual_trigger_preflight()` → run-scoped temp dir（mode `0700`）確保 →
`build_repository_collector()`（Child 3 `collect_snapshot.py` の `collect_repository_source` を
`base_sha` 単一引数の closure へ束縛）を含む collectors を `prepare()` に渡し、`run_id`（run-scoped
nonce）と `base_sha`（一度だけ解決、以降再解決しない）を固定した `RunContext` と `SourcePlan` を得る。

### 3. observer wave（fan-out/fan-in の全 observer 終端バリア。`observer_parallelism: 3`、
`EXPECTED_OBSERVER_MANIFEST` は固定 3 件、Issue #2646 で追加）

`build_observer_requests()` で以下 3 observer の `AgentInvocationRequest` を組み立て、
`invoke_agent()`（headless CLI subprocess `claude -p --agent <name> --output-format json
--json-schema <schema 本文> --no-session-persistence`、prompt は stdin 経由）で呼び出す:

- `retrospective-runtime-observer`（interpreter role。Claude Code/Claude-GPT session evidence の解釈専用）
- `codebase-investigator`（既存 SubAgent の再利用。advisory role、base_sha 非束縛の調査は finding
  authority にしない -- `finding_authority: advisory` タグが付与される）。substantive な
  caller-supplied task（`--prompts-file` 経由）を持つ場合にのみ `agy_advisory_native_fallback_allowed:
  true` と `authoritative_base_sha=ctx.base_sha` を明示的に配線し、この場合に限り `--json-schema` へ渡す
  schema も `codebase_investigation_result_v1.schema.json`（native 契約自身の schema）へ切り替える
  （default/no-task path、他の2 observer には配線せず、常に `observer_result_v1.schema.json` のまま --
  Issue #2374、PR #2387 review fix_delta P0-1: 二方式を混在させず role_adapter の有無で決定論的に分岐）。
  role adapter（`apply_codebase_investigator_role_adapter`）が AGY operational failure 後の native
  fallback 結果（`CODEBASE_INVESTIGATION_RESULT_V1`）を実 `jsonschema.validate` で検証（native `status:
  failed`/`inconclusive`、または `evidence_refs` の `commit_sha != ctx.base_sha` は typed failure とし
  空 findings の成功へ変換しない）し、さらに各 `evidence_refs` エントリのバイト内容を
  `git show <base_sha>:<path>` による独立再検証（`gemini-cli-headless-delegation` skill の
  `validate_repo_evidence_ref` を再利用、read-only git コマンドのみ）で確認した上で
  `EvidenceBundle`/`OBSERVER_RESULT_V1` へ正規化する。
- `web-researcher`（既存 SubAgent の再利用。discovery role。`evidence_digest` が Web collector の
  再取得済み digest と一致しない場合は `UnboundEvidenceAuthority` で reject される）

`run_observer_wave()` は required observer 3 体を、先に dispatch した observer の completion を
待たずに同時 dispatch する（fan-out）。1 体が通常失敗（malformed/schema 不一致/nonzero exit 等）
しても他 observer の dispatch/completion は止まらず、全 3 体が terminal になるまで待つ（fan-in、
all-terminal barrier）。各 observer の返却 `EvidenceBundle`（`OBSERVER_RESULT_V1`）は strict
validation され、`bundle.observer_id == request.agent_name`（返却 payload 側ではなく起動要求側の
observer_id を identity authority とする）・`ctx.base_sha` との一致・observer_id 重複なし・manifest
完全一致を検証する。observer_id の不一致（入れ替え）は `observer_id_mismatch` という独立した terminal
failure として扱われ、他 observer の回収は継続し、evaluator は起動しない。失敗時は
`schema_repair_retries: 1` まで repair を試み、それでも失敗すれば **evaluator を起動せず**
fail-closed で終了する（AC14）。1 observer のみの失敗は既存の granular `reason_code` を維持し、
複数 observer が同時に失敗した場合は `observer_wave_multiple_failures` という専用の top-level
`reason_code` を用いて、単一の「最初の失敗理由」で他の失敗を隠さない。全 observer の
`status`/`reason_code`/`exit_code` は aggregate として `main()` の呼び出し元まで取得可能（CLI 失敗
JSON の `observer_results` フィールド）。

observer 自身の timeout では、その observer の実 subprocess を terminate → 有限の猶予 → 必要なら
kill → reap してから terminal record を確定し、他 observer は継続する。親プロセスへの
SIGINT/SIGTERM では、起動済みの全 observer 子プロセスへ終了要求を出し、有限の猶予後に必要なら
kill し、全ての子プロセスの終了・reap を確認してから run-scoped temporary directory の cleanup を
実行する（`run_scoped_temp_dir()`/`terminate_all_active_child_processes()`）。SIGINT/SIGTERM の
Python シグナルハンドラ自体は「中断要求の記録＋即時 raise」のみに限定され（Lock 取得やブロッキング
待機を一切行わない）、実際の terminate → 猶予 → kill → reap 収束は `run_scoped_temp_dir()` 自身の
`finally`（通常の制御フロー）で行う。これは evaluator のように main thread が自身の子プロセスを
同期呼び出し中に割り込まれるケースで「自分自身の reap 完了を自分で待つ」自己待機を構造的に防ぐため
（Issue #2646 PR #2650 fix_delta）。詳細は `references/execution-budget.md` を参照。

`DelegatedAgentPermissionPolicy`（`run_retrospective.py`）が実際の subprocess argv（`--disallowedTools`）
と subprocess env（mutation credential を除去した allowlist）へ直接反映され、`git commit`/`git push`/
`gh issue`/`gh pr`/filesystem write/unapproved Bash/対象 run 外 resume を拒否する。

#### headless CLI 標準出力の形状正規化処理（transport normalization、Issue #2645 対応）

`invoke_agent()` は `completed.stdout` を `json.loads()` した直後、既存の result-wrapper business
validation（type/subtype/is_error 判定、`structured_output` compatibility recovery、JSON Schema
validation、identity binding、role adapter validation）へ渡す**前段**で、`_normalize_transport_payload()`
（薄い transport normalization adapter）を通す。

- 単一 object payload（従来からの唯一の正当な shape）はそのまま無変更で通過する（regression-free）。
- 正当な event-array payload（`system`/`assistant`/`tool_use`/`tool_result` 等の非 terminal event に続き、
  一意な terminal `type: "result"` event が **配列末尾** に存在する shape）は、その terminal event の
  dict をそのまま canonical result object として抽出し、既存 business validation へそのまま渡す。
  terminal event が 0 件・複数件（success/failure subtype で絞り込む前にカウントする -- success 1件 +
  error 1件 の配列は曖昧として拒否）・terminal event の後に継続 event が存在する・配列要素が object で
  ない、のいずれかの場合は fail-closed のまま維持される（`payload[-1]` の機械的採用や、nested JSON の
  recursive search による business result 採取は行わない）。
- 単一 object でも配列でもない top-level JSON 値（文字列・数値・null 等）は、従来どおり
  `reason_code="payload_not_object"` のまま fail-closed。

**fact-check（原因調査自体は拡大しない、In Scope）**: 配列 shape の発生は Claude-GPT/self-hosted
固有と断定できない。native Claude Code 側の公開 Issue
（https://github.com/anthropics/claude-code/issues/84784）は、`--verbose`（および公式 CLI reference
https://code.claude.com/docs/en/cli-reference が記す、それを上書きする `viewMode: "verbose"` 設定）が
native Claude Code 自身の `--output-format json` を単一 object から JSON 配列へ変える既知挙動を報告して
いる。`_normalize_transport_payload()` はこの事実を踏まえ、どの launcher/brand が発生させたかではなく
observed な top-level JSON shape のみを見て分岐する runtime-agnostic 実装である。これは
`--output-format stream-json`（改行区切り JSON/NDJSON）とは無関係 -- NDJSON の top-level
`json.loads()` は本 adapter に到達する前に `json_decode_failure` で fail-closed する。

### 4. prepare-evaluator（評価準備・fan-in）

全 observer が成功した場合のみ（`partial_agent_output: reject`）、
`build_finding_sets()`（observer role から `finding_authority` を導出・`web` finding の digest 束縛を検証）
→ `prepare_evaluator_request()` で `EvaluatorRequest`（schema-controlled projection のみ、raw evidence
を含まない）を組み立てる。

### 5. evaluator 起動（observer wave 完了後にのみ、observer と同時起動しない）

fresh context で `retrospective-evaluator` を headless CLI subprocess で 1 回起動し、
`run_evaluation()` で `Evaluation`（`EVALUATION_RESULT_V1`）を strict validation する。
`candidate_records` は現行マージ済み `agent_improvement_candidate/v1`（#2288/#2289）の canonical
schema を満たさない限り reject される。`evaluator_retries: 0`（再試行しない）。

### 6. delta 算出（`execute_run()`/`run_cli()` の production call graph に配線済み、Issue #2237
fix_delta iteration-4 Warning 1）

`execute_run()`/`run_cli()` は evaluator 起動直後に `previous_state_provider`（未指定時は空
`FixturePreviousStateProvider(fixtures={})`）の `get()` を呼び、`available`/`no_history`/
`legacy_unavailable`/`partial`/`stale` の 5 状態から `compute_delta()` で `new`/`resolved`/
`recurrent`/`regressed`/`unchanged`（`finding_contract.identity`/`evaluations[]` に基づく -- legacy
`candidate_status` 由来ではない）を算出し、結果を `finalize(..., delta_results=...)` 経由で
`PublishRequest.delta_results` に格納する。`partial`/`stale` は indeterminate を強制する。この
delta 算出 step 自体は毎回実行される（"任意" なのは provider を差し替えるかどうかであり、呼び出す
かどうかではない）。production provider（実際の永続化読み取り）は #2238 の責務だが、`execute_run()`/
`run_cli()` はどちらも `PreviousStateProviderProtocol` を満たす任意の provider を受け取れるため、
#2238 はこの call graph 自体を変更せず real provider を注入できる。

### 7. 確定処理（finalize）

`finalize()` で proposal-only `PublishRequest`（`PUBLISH_REQUEST_V1`）を生成する。
`public_projection_digest` は `run_identity`（`source_set_digest` を含む）と
`expected_previous_digest`（concurrency token）に束縛される。
`authorized`/`authorized_by_human`/`authorization_token`/`mutation_capability` はスキーマレベルで禁止
フィールドであり、`PublishRequest` dataclass には存在しない（AC16）。人間承認・実際の Issue mutation は
別の trusted channel（`HUMAN_AUTHORIZATION_RECEIPT_V1`、#2238）が担う。`run_cli()` はこの
`PublishRequest` を返し、`main()` がその `to_wire()` 文字列を stdout へ出力する。

## Reused Agents（再利用エージェント・capability matrix）

| Role | Authority | 再利用元 |
|---|---|---|
| runtime observer | interpreter（private runtime evidence + digest） | `.claude/agents/retrospective-runtime-observer.md`（新規） |
| codebase investigator | advisory | `.claude/agents/codebase-investigator.md`（既存再利用） |
| web researcher | URL discovery / claim interpretation | `.claude/agents/web-researcher.md`（既存再利用） |
| evaluator | privileged synthesis（validated projection のみ） | `.claude/agents/retrospective-evaluator.md`（新規） |

詳細な enforcement contract（本番制約・必須入力）は `references/capability-matrix.md` を参照。

## 実行 budget（要約）

```yaml
observer_parallelism: 3
schema_repair_retries: 1
evaluator_retries: 0
partial_agent_output: reject
timeout_status: typed operational failure
interruption_status: aborted
cleanup_required: true
```

詳細は `references/execution-budget.md` を参照。`maxTurns`・出力上限などの具体値は
`.claude/agents/retrospective-runtime-observer.md` / `.claude/agents/retrospective-evaluator.md`
の frontmatter に固定されている。

## Ephemeral wire contract（一時的な通信契約）

`SOURCE_PLAN_V1` / `OBSERVER_RESULT_V1` / `FINDING_SET_V1` / `EVALUATOR_REQUEST_V1` /
`EVALUATION_RESULT_V1` / `PUBLISH_REQUEST_V1` の 6 envelope。詳細フィールド定義は
`references/wire-contract.md` を参照。すべて `schema_version`/`run_id`/`base_sha`/`source_set_digest`
必須（`PUBLISH_REQUEST_V1` は `run_identity` object 経由）、未知フィールド拒否、oversize 拒否、
schema repair retry 上限 1。

## 永続化（run の実データを Issue comment として保存する。実装は Issue #2238 / Child 5）

`run_retrospective.py main()` は `--state-backend` 引数（既定 `issue-comments`、Issue #2238 P1-3
fix_delta）で `PreviousStateProvider` backend を選択する。`issue-comments`（既定・本番）は
`resolve_previous_state_provider()` が sibling module `persist_retrospective_run.py` の
`IssueCommentPreviousStateProvider` を実際に構築し `run_cli()` へ注入する -- `gh auth` が使えない場合は
`fixture` へ暗黙 fallback せず、型付き `GhAuthUnavailable` で fail-closed する。`fixture` backend
（空の `FixturePreviousStateProvider`）が本当に必要な場合は `--state-backend fixture` を明示する。
provider の `read_version`（直近 publication の digest）は `execute_run()`/`run_cli()` の
`finalize()` 呼び出しへ `expected_previous_digest` として伝播する。`finalize()` は同時に、Child 3 の
実際の per-collector `source_observations[]`（および `generated_at`/`runtime_version`）を
`run_identity` へ additive に格納する（Issue #2238 P0-5 fix_delta -- 固定 placeholder ではなく
実データ）。

`persist_retrospective_run.py` は `run_retrospective.py` が生成した `PUBLISH_REQUEST_V1` を消費し、
`agent_retrospective_run_publication/v1` envelope（`run_identity` + 実際の `source_observations[]` +
`candidate_records` + `delta_results`、`sha256-sorted-json-v1` 準拠 `publication_digest` 付き）を
構築する。CLI は 2 段階フロー（Issue #2238 P0-2 fix_delta）で公開する:

1. `prepare-publication` サブコマンド: `repository_id` と `--repo` の cross-check（不一致なら
   transport 呼び出しゼロで拒否）→ stable `request_payload_digest`（`parent_record_digest`/
   `generated_at` を含まない）による idempotency 判定（`no_op`/`conflict` はここで終端、authorization
   不要）→ `publish` 決定のみ optimistic concurrency precheck（best-effort、ADR 0007 Decision 5、
   `expected_previous_digest=None` は「head が None であること」を要求する厳格値）+ public-safety
   validator（field allowlist + 値レベル credential/token/absolute-path パターン拒否 + size 事前
   確認）→ envelope と `publication_digest` を file へ固定
2. `authorize` サブコマンド: frozen file を人間へ提示し（TTY 明示確認）、または frozen file を参照する
   `human_authorization_receipt/v1` を発行する（TTL <= 10分、`approved_at` の未来日時・TTL 超過は
   拒否）
3. `publish` サブコマンド: POST 直前に live head を再検証し、`prepare-publication` 時点から変化して
   いれば POST せず再実行を要求する → human authorization gate 確認 → POST（ambiguous failure から
   の `request_id`/idempotency-key ベース回収を毎回の ambiguous 結果ごとに実施）→ post-write
   readback（comment ID で GET → 同一 author allowlist/schema/digest 検証関数で再検証、失敗時は
   `published_unverified` で停止し index 更新を呼ばない）+ sibling rescan（同一
   `parent_record_digest` を持つ comment が複数あれば `conflict_detected`。永続的な帰結は
   `IssueCommentPreviousStateProvider.get()` の read-time fork 再構成が担う）→ 任意の
   `--index-parent-issue` 指定時は `scripts/agent-logs/update-retro-index.mjs` を実行、失敗しても
   一次記録はロールバックせず `published_index_stale` を返す

単発呼び出し（`publish_run()`／サブコマンド無しの legacy CLI 呼び出し）は上記 3 段を 1 回で合成する
薄い wrapper として残る。

### `human_authorization_receipt/v1`

```yaml
human_authorization_receipt/v1:
  request_id: <string>            # PUBLISH_REQUEST_V1.request_id と一致必須
  publication_digest: <sha256:..> # 承認対象の envelope digest と一致必須
  repository_id: <owner/repo>
  target_issue: <int>
  operation: "publish_retrospective_run"
  approved_at: <ISO 8601>
  expires_at: <ISO 8601>          # 検証時刻がこれ以降なら拒否（fail-closed）
                                   # (approved_at, expires_at) の幅は 10分以内（Issue #2238 P0-2）
```

既存 `PUBLISH_REQUEST_V1` の禁止 field（`authorized`/`authorized_by_human`/`authorization_token`/
`mutation_capability`）は維持されたまま。承認は本 receipt ファイル、または TTY 明示確認という
別チャネルでのみ確認する。

## Latitude ランタイム証跡（latitude_runtime_evidence/v1、Issue #2375）

`agent-retrospective` は Latitude CLI から bounded・read-only な runtime evidence を任意で
収集し、`runtime_behavior` claim_class の finding へ deterministic に enrichment できる（Latitude
不在・不許可・利用不能は retrospective 自体を止めない）。

- 収集: `scripts/collect_snapshot.py`'s `collect_latitude_runtime_evidence()`（実 CLI
  `latitude traces list --project-slug <slug> --filters <JSON> --limit <n> --format json` の
  argv-only、最大 1 回起動、timeout 10秒、output 64 KiB 上限、allowlisted metric 3 個。
  `project_slug` は `LATITUDE_PROJECT` 環境変数からのみ解決する）
- Session Correlation: `scripts/run_retrospective.py`'s
  `_resolve_latitude_target_session_id()` が既存の hook-sink 収集経路（Claude-GPT adapter の
  `complete_sessions`）から対象 Claude Code `session_id` を解決し、`--filters` の
  `sessionId eq` 条件として渡す。「直近 trace を無条件取得」するフォールバックは行わない。
  `session_id` 未解決または相関 0 件は `unavailable`（`session_id_unresolved`/
  `no_matching_trace`）に縮退する。
- 実行経路への配線: `scripts/run_retrospective.py`'s `execute_run()` が
  `collect_latitude_runtime_evidence_once()`/`bind_latitude_evidence_to_candidates()` を
  `compute_delta()` 直後・`finalize()` 直前に呼び出す（Collection Budget: 1 run あたり
  `latitude` CLI 起動は最大 1 回。失敗・利用不能は retrospective 全体を止めない）。
- 検証: `scripts/validate_retrospective_schema.py`'s `validate_latitude_runtime_evidence()`
  （`schemas/latitude_runtime_evidence_v1.schema.json`。closed key set、availability 別
  nullability、closed reason_code、identity 再計算による mismatch fail-closed）
- 結合: `scripts/run_retrospective.py`'s `bind_latitude_evidence_to_candidates()`（available
  evidence のみ、`claim_class == "runtime_behavior"` の候補のみ、既存 `evidence_ref` の
  `runtime_receipt`/`runtime` shape で `evidence_refs[]` に追記するだけ -- claim/status/confidence
  は一切変更しない）
- CLI Boundary・Collection Budget の詳細は `references/wire-contract.md` /
  `references/execution-budget.md` を参照。
- 動作検証（opt-in）: `scripts/tests/verify_latitude_runtime_evidence_live_cli.py`
  （`pytest.skip()`、stdout `SKIP:` prefix。CLI/認証/network unavailable は SKIP、予期しない
  失敗は FAIL、成功時は public-safe artifact を生成して PASS）
- Latitude は public `source_kind` にしない（`latitude_otlp` は既存 #1223 の private provenance
  のまま。`agent_run_report/v1` の public `source_kind` enum は変更しない）。

## 私設ローカル監査 resolver（retro_private_audit_index/v1、Issue #2376、#1939 Workstream 5）

既存の public-safe `evidence_ref`（`agent_improvement_candidate_v1` schema）と publication
`run_identity`（`run_id`/`base_sha`/`source_set_digest`）から、private local audit evidence を
fail-closed に解決・保存する resolver。v1 は project Skill only（`plugins/agent-retrospective/...`
への mirror なし）。

- resolver: `scripts/private_audit_resolver.py`'s `resolve()`/`resolution_key()`/
  `manifest_digest()`/`register_private_audit_ref()`/`write_manifest()`。
- Identity 分離（AC2）: `resolution_key`（`run_identity`+`evidence_ref` のみから決定論的に導出
  される stable identity）と `manifest_digest`（generation snapshot -- `private_status_at_generation`/
  `reason_code`/`object_key`/`object_digest`/`expires_at` -- をbindする digest）の2層。access時の
  再評価（source missing/permission changed/digest mismatch/expired）はどちらも書き換えない。
- Availability は `available | unavailable` の2値のみ（AC3/AC8）。missing/malformed/digest
  mismatch/permission mismatch/expired はすべて fail-closed `unavailable` に畳み込む。sibling
  `latitude_runtime_evidence/v1`（3値 `available | unavailable | error`）契約は変更しない。
- Storage: atomic write（`tempfile.mkstemp()` -> `os.replace()`）+ `0600` permission、audit root
  からの opaque relative `object_key`（絶対パスは一切保存しない）。resolver 自身によるこの
  local-only storage read/write は明示的に許可された core functionality。
- Producer hook: `scripts/run_retrospective.py`'s `register_private_audit_ref()`（`execute_run()`
  内、`compute_delta()`/Latitude binding の直後・`finalize()` の直前で呼び出す）。THIS 実行で
  local private source（実際に収集済みの real evidence data）が既に存在する `evidence_ref` に
  ついてのみ sidecar mapping を登録する -- 存在しない場合は何も書かない（fabricate しない）。
  best-effort・fail-open（失敗しても retrospective 本体は止めない）。
- Expiry: `expires_at: RFC3339 UTC | null`（`null` は resolver 独自の expiry なし）。access 時に
  local resolver が lazy 評価するのみで、background daemon/cleanup service は追加しない。
- 再利用: canonical JSON は既存 `json.dumps(value, sort_keys=True, separators=(",", ":"))` パターン
  （`validate_retrospective_schema.compute_source_set_digest()` 等と同一呼び出し）を再利用し、
  RFC3339 date-time format checking は既存 `validate_retrospective_schema._validate_with_format_checking()`
  （module-local stdlib-only FormatChecker）を再利用する。いずれも新規 canonicalization/validation
  infrastructure は追加しない。
- Schema/fixtures: `schemas/retro_private_audit_index_v1.schema.json`（closed schema、
  `additionalProperties: false`、bounded enum、free-form instruction field なし）/
  `schemas/fixtures/retro_private_audit_index_v1.*.json`。public-safety 再検証（raw transcript/
  prompt/tool I/O/stdout/stderr/credential/token/secret/absolute local path の混入拒否）は既存
  `scripts/tests/test_public_safe_evidence_refs.py` の parametrized 検証を再利用・拡張する。
- Public evidence_ref 単体では claim 真偽を立証できない: `resolve()` はローカル filesystem 上の
  `audit_root` への実アクセスを必須とし、`evidence_ref`/`run_identity` のみから availability を
  返す public-only 経路は存在しない。ChatGPT 等の GitHub-only reader が既存 public artifact のみ
  から claim 真偽を独自判定する手段は提供しない。

## since-last-retrospective の checkpoint 確定（Issue #2644）

`run_retrospective.py --since-last-retrospective` の checkpoint/watermark 書き戻しは、収集・coverage
計算（`collect_session_sources()`/`build_session_window_coverage_result()`）の直後ではなく、
`analysis_runner`（`--enable-full-analysis` 指定時は `build_since_last_analysis_runner()` 経由の
`run_cli()` 呼び出し）が observer/evaluator/finalize まで成功した後にのみ行われる。同一 run で
固定した window・選択 session 集合・collector 結果は `build_runtime_observer_task_prompt()` を通じて
`retrospective-runtime-observer` の実プロンプトへそのまま渡され、`run_cli()` の
`current_source_coverage` 引数として `compute_delta()` / canonical `finding_contract.evaluations[]`
生成経路の両方に同一の current source coverage 判定入力として伝播する。observer/evaluator/finalize が
失敗した場合は `checkpoint_advanced: false` / `checkpoint_advance_reason: "blocked_evaluation_failure"`
となり watermark ファイルへの書き込みは行われない（次回実行で同じ未分析 window が再選択される）。

`--enable-full-analysis` を指定しない coverage-only invocation（`analysis_runner` 未接続）は
observer/evaluator/finalize を一切実行しないため、分析済みを表す durable watermark を進めてはならない
（PR #2660 fix_delta P1-1: OWNER REQUEST_CHANGES）。収集・coverage の disposition 単体が advance 相当
だった場合でも、`run_since_last_retrospective_cli()` は `checkpoint_advanced` を `false`・
`checkpoint_advance_reason` を `blocked_evaluation_failure` へ強制的に再計算し、watermark ファイルへは
書き込まない（新しい `checkpoint_advance_reason` enum 値は追加せず、「未実行」と「実行して失敗」を同一の
既存値で表現する）。`checkpoint_advanced == true` は、計算上の advance 判定ではなく実際の watermark
ファイル書き込みが成功した後にのみ成立する。

`analysis_runner` が成功した場合に生成される proposal-only `PublishRequest` は捨てられない（PR #2660
fix_delta P1-2）。`run_since_last_retrospective_cli()` の `analysis_result_sink` 引数（caller が渡す
list）へ append され、`main()` は checkpoint/watermark commit 後、既存の `PublishRequest.to_wire()`
シリアライズ経路（default mode の `print(publish_request.to_wire())` と同一）を再利用して stdout の
追加行へ出力する。新規 DB・publisher framework・persistence subsystem・permission broker は追加しない。

**checkpointの完了はGitHub投稿の成功と区別する。** 既存の `publish_authorized` 事前 authorization
判定（`compute_checkpoint_disposition()` が参照する）は checkpoint 確定の入力の一つだが、
`finalize()` 完了後に別処理が行う実際の GitHub comment/post（publication、`persist_retrospective_run.py`
の責務）の成功・失敗は checkpoint 確定の入力に一切含めない。`analysis_runner` は `run_cli()`/
`finalize()` が返す proposal-only `PublishRequest` の生成・受け渡しの完了までを指し、GitHub への
実際の投稿呼び出しは行わない。

## Guardrails（ガードレール）

- **Allowed Paths 外を編集しない**
- `run_retrospective.py` は GitHub/Issue へのいかなる mutation も実行しない（proposal-only）
- observer/evaluator は leaf SubAgent（`tools` に `Agent`/`Skill` を含まない、nested delegation 禁止）
- evaluator は observer wave 完了・validated projection 受領前には起動しない
- raw evidence（stdout/stderr/絶対パス/credential）は `evidence_ref` 以外の形で wire envelope を通過しない
- Bash を保持する observer（`codebase-investigator`）の git/gh mutation は、`--disallowedTools`（Write/Edit/
  MultiEdit/NotebookEdit/Agent/Skill の tool 名denyのみで、Bash 自体は対象外）だけでは防げない。
  `run_cli()` が run-scoped `--settings` ファイル（`write_bash_guard_settings_file`）経由で real
  `PreToolUse` hook（`retrospective_bash_guard_hook.py`）を注入し、`DelegatedAgentPermissionPolicy
  .check_bash`（`read_only_investigation_enabled=True`）が実際の tool call 実行前に git/gh の
  mutating subcommand を拒否する（Issue #2419 -- 修正前は `check_bash` が本番呼び出し経路のどこからも
  呼ばれない dead code で、observer が Bash 経由の `git merge` を実行し canonical local `main` を
  破損させる実害が発生した）。agent frontmatter 自身の `hooks:` フィールドは headless `-p` session
  では発火しない（workspace trust dialog が `-p` では成立しないため）ので、この用途には使わない。
  この read-only investigation profile は PR #2425 review fix_delta（#2425#issuecomment-5466916997）で
  3 つの明示 capability の allowlist に再構成されている: (1) canonical AGY builder/wrapper invocation
  （`build_request.py`/`run_gemini_headless.py --request-file <path> --output-file <path>` の正規パス
  への `Path.resolve()` 完全一致（repo-root anchored、PR #2425 review fix_delta round 4）のみ。それ以外の
  `python3`/`uv` 呼び出しはすべて拒否）、(2) native Git read-only subcommand allowlist（`show/log/diff/
  blame/rev-parse/status/cat-file/ls-tree/grep/merge-base` の argv POSITION ベース判定。未列挙 mutation
  はすべて拒否される denylist 逆転設計）、(3) native GitHub `(group, action)` exact pair allowlist +
  `gh api` GET-only 判定（argv POSITION ベース。flag 値や branch 名が action token と一致しても bypass
  しない）。shell 側は quote-aware tokenizer（`shlex.shlex(punctuation_chars=...)`）で `|` のみをパイプ
  演算子として許可し、`;`/`&`/`&&`/`||`/改行はトークンとして出現した時点で無条件拒否する（クォート内の
  `|` を誤って区切り文字として扱わない）。`` ` ``/`$(`/`<(`/`>(`（command/process substitution）は生文字列
  レベルで無条件拒否する。hook 自身の内部例外（import error 等）は `main()` 全体の try/except で捕捉し
  `stderr` に診断出力した上で `sys.exit(2)` する（Claude Code の PreToolUse hook 契約では exit 2 のみが
  block、他の非0終了は non-blocking error として tool 実行が続行されるため）。

## Related（関連情報）

- `.claude/skills/agent-retrospective/scripts/collect_snapshot.py`（Child 3、#2236）
- `.claude/skills/agent-retrospective/scripts/validate_retrospective_schema.py`（Child 2、#2235/#2288）
- `.claude/skills/agent-retrospective/scripts/persist_retrospective_run.py`（Child 5、#2238）
- `.claude/skills/agent-retrospective/scripts/tests/verify_latitude_runtime_evidence_live_cli.py`（Issue #2375 AC6 opt-in 動作検証）
- `.claude/skills/agent-retrospective/references/wire-contract.md`（永続化 envelope の詳細）
- `.claude/agents/codebase-investigator.md` / `.claude/agents/web-researcher.md`（既存再利用）
- `docs/adr/0007-agent-retrospective-boundaries.md`
- `docs/dev/agent-skill-boundaries.md`
- `docs/dev/runtime-verification-policy.md`

## 出力制約 (OUTPUT_BUDGET_V1)

`docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` の制約に従う。routing-critical な機械可読
フィールドは削らず、人間向け説明・証跡・diff 再掲のみを削減する。
