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
  --schema-dir <observer/evaluator 用 JSON Schema ファイルの配置ディレクトリ> \
  --prompts-file <observer_id -> prompt テキストの JSON ファイル>
```

root Skill が用意するのはこの 1 回の Bash 呼び出しのみ。内部で `run_retrospective.run_cli()` が
以下を順に実行する（各関数の詳細は `run_retrospective.py` 本体の docstring を正本とする）。

### 2. 準備（prepare）

`manual_trigger_preflight()` → run-scoped temp dir（mode `0700`）確保 →
`build_repository_collector()`（Child 3 `collect_snapshot.py` の `collect_repository_source` を
`base_sha` 単一引数の closure へ束縛）を含む collectors を `prepare()` に渡し、`run_id`（run-scoped
nonce）と `base_sha`（一度だけ解決、以降再解決しない）を固定した `RunContext` と `SourcePlan` を得る。

### 3. observer wave（fan-out、`observer_parallelism: 3`、`EXPECTED_OBSERVER_MANIFEST` 固定 3 件）

`build_observer_requests()` で以下 3 observer の `AgentInvocationRequest` を組み立て、
`invoke_agent()`（headless CLI subprocess `claude -p --agent <name> --output-format json
--json-schema <schema 本文> --no-session-persistence`、prompt は stdin 経由）で呼び出す:

- `retrospective-runtime-observer`（interpreter role。Claude Code/Claude-GPT session evidence の解釈専用）
- `codebase-investigator`（既存 SubAgent の再利用。advisory role、base_sha 非束縛の調査は finding
  authority にしない -- `finding_authority: advisory` タグが付与される）
- `web-researcher`（既存 SubAgent の再利用。discovery role。`evidence_digest` が Web collector の
  再取得済み digest と一致しない場合は `UnboundEvidenceAuthority` で reject される）

`run_observer_wave()` が `EvidenceBundle`（`OBSERVER_RESULT_V1`）へ strict validation し、
`ctx.base_sha` との一致・observer_id 重複なし・manifest 完全一致を検証する。失敗時は
`schema_repair_retries: 1` まで repair を試み、それでも失敗すれば **evaluator を起動せず**
fail-closed で終了する（AC14）。

`DelegatedAgentPermissionPolicy`（`run_retrospective.py`）が実際の subprocess argv（`--disallowedTools`）
と subprocess env（mutation credential を除去した allowlist）へ直接反映され、`git commit`/`git push`/
`gh issue`/`gh pr`/filesystem write/unapproved Bash/対象 run 外 resume を拒否する。

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

`run_retrospective.py main()` は `--state-backend` 引数（既定 `fixture`）で `PreviousStateProvider`
backend を選択する。`--state-backend issue-comments` を指定すると、`resolve_previous_state_provider()`
が sibling module `persist_retrospective_run.py` の `IssueCommentPreviousStateProvider` を実際に
構築し `run_cli()` へ注入する（`fixture` は従来通り空の `FixturePreviousStateProvider`）。provider の
`read_version`（直近 publication の digest）は `execute_run()`/`run_cli()` の `finalize()` 呼び出しへ
`expected_previous_digest` として伝播する。

`persist_retrospective_run.py` は `run_retrospective.py` が生成した `PUBLISH_REQUEST_V1` を消費し、
`agent_retrospective_run_publication/v1` envelope（`run_identity` + 単一 `source_observations` 項目 +
`candidate_records` + `delta_results`、`sha256-jcs-v1` 準拠 `publication_digest` 付き）を構築して、
以下を順に実行する:

1. **optimistic concurrency precheck**（best-effort、ADR 0007 Decision 5）: POST 前に現在の head
   digest を再確認し `parent_record_digest` として束縛する
2. **public-safety validator**: field allowlist（未知 top-level key 拒否）+ 値レベルの
   credential/token/absolute-path パターン拒否 + size 事前確認。違反時は POST しない
3. **idempotency guard**: `(repository_id, base_sha, source_set_digest, scope)` から publisher が
   自前で再計算した key による `no_op`/`conflict`/`publish` の三値判定
4. **human authorization gate**（fail-closed）: TTY 明示確認、または別コマンドが発行する短命な
   `human_authorization_receipt/v1` ファイルの検証のいずれかが必須。単独の `--authorized-by-human`
   相当の flag は存在しない
5. **POST**（ambiguous failure からの `request_id`/idempotency-key ベース回収を含む）
6. **post-write readback**（comment ID で GET → canonical JSON digest 再計算 → 一致確認）と
   sibling rescan（同一 `parent_record_digest` を持つ comment が複数あれば `conflict_detected`）
7. 任意の `agent_retro_index/v1` derived-index 更新。失敗しても一次記録はロールバックせず
   `published_index_stale` を返す

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
```

既存 `PUBLISH_REQUEST_V1` の禁止 field（`authorized`/`authorized_by_human`/`authorization_token`/
`mutation_capability`）は維持されたまま。承認は本 receipt ファイル、または TTY 明示確認という
別チャネルでのみ確認する。

## Guardrails（ガードレール）

- **Allowed Paths 外を編集しない**
- `run_retrospective.py` は GitHub/Issue へのいかなる mutation も実行しない（proposal-only）
- observer/evaluator は leaf SubAgent（`tools` に `Agent`/`Skill` を含まない、nested delegation 禁止）
- evaluator は observer wave 完了・validated projection 受領前には起動しない
- raw evidence（stdout/stderr/絶対パス/credential）は `evidence_ref` 以外の形で wire envelope を通過しない

## Related（関連情報）

- `.claude/skills/agent-retrospective/scripts/collect_snapshot.py`（Child 3、#2236）
- `.claude/skills/agent-retrospective/scripts/validate_retrospective_schema.py`（Child 2、#2235/#2288）
- `.claude/skills/agent-retrospective/scripts/persist_retrospective_run.py`（Child 5、#2238）
- `.claude/skills/agent-retrospective/references/wire-contract.md`（永続化 envelope の詳細）
- `.claude/agents/codebase-investigator.md` / `.claude/agents/web-researcher.md`（既存再利用）
- `docs/adr/0007-agent-retrospective-boundaries.md`
- `docs/dev/agent-skill-boundaries.md`
- `docs/dev/runtime-verification-policy.md`

## 出力制約 (OUTPUT_BUDGET_V1)

`docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` の制約に従う。routing-critical な機械可読
フィールドは削らず、人間向け説明・証跡・diff 再掲のみを削減する。
