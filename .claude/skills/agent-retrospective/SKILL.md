---
name: agent-retrospective
description: Claude Code / Claude-GPT の run 証跡から改善候補（agent_improvement_candidate/v1）を proposal-only で生成する継続的 retrospective の orchestrator。人間の明示トリガーでのみ起動する（自動起動しない）。
disable-model-invocation: true
---

# Agent Retrospective (Orchestrator)

`.claude/skills/agent-retrospective/` は、Claude Code / Claude-GPT のセッション証跡・repository・GitHub・Web
の 5 source から収集した evidence を、独立 2 段の SubAgent（observer wave → evaluator）で解釈し、
`agent_improvement_candidate/v1`（#2288 で delta-evaluation 拡張済み）準拠の改善候補を **proposal-only** で
生成する。GitHub Issue への実際の投稿・mutation は本 Skill のスコープ外（#2238）。

## Orchestration owner

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

### 2. prepare

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

### 4. prepare-evaluator（fan-in）

全 observer が成功した場合のみ（`partial_agent_output: reject`）、
`build_finding_sets()`（observer role から `finding_authority` を導出・`web` finding の digest 束縛を検証）
→ `prepare_evaluator_request()` で `EvaluatorRequest`（schema-controlled projection のみ、raw evidence
を含まない）を組み立てる。

### 5. evaluator 起動（observer wave 完了後にのみ、observer と同時起動しない）

fresh context で `retrospective-evaluator` を headless CLI subprocess で 1 回起動し、
`run_evaluation()` で `Evaluation`（`EVALUATION_RESULT_V1`）を strict validation する。
`candidate_records` は現行マージ済み `agent_improvement_candidate/v1`（#2288/#2289）の canonical
schema を満たさない限り reject される。`evaluator_retries: 0`（再試行しない）。

### 6. delta 算出（任意、`PreviousStateProvider` 経由）

`FixturePreviousStateProvider` を使い、`available`/`no_history`/`legacy_unavailable`/`partial`/`stale`
の 5 状態から `compute_delta()` で `new`/`resolved`/`recurrent`/`regressed`/`unchanged`
（`finding_contract.identity`/`evaluations[]` に基づく -- legacy `candidate_status` 由来ではない）
を算出する。`partial`/`stale` は indeterminate を強制する。production provider（実際の永続化読み取り）
は #2238 の責務。

### 7. finalize

`finalize()` で proposal-only `PublishRequest`（`PUBLISH_REQUEST_V1`）を生成する。
`public_projection_digest` は `run_identity`（`source_set_digest` を含む）と
`expected_previous_digest`（concurrency token）に束縛される。
`authorized`/`authorized_by_human`/`authorization_token`/`mutation_capability` はスキーマレベルで禁止
フィールドであり、`PublishRequest` dataclass には存在しない（AC16）。人間承認・実際の Issue mutation は
別の trusted channel（`HUMAN_AUTHORIZATION_RECEIPT_V1`、#2238）が担う。`run_cli()` はこの
`PublishRequest` を返し、`main()` がその `to_wire()` 文字列を stdout へ出力する。

## Reused Agents（capability matrix）

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

## Ephemeral wire contract

`SOURCE_PLAN_V1` / `OBSERVER_RESULT_V1` / `FINDING_SET_V1` / `EVALUATOR_REQUEST_V1` /
`EVALUATION_RESULT_V1` / `PUBLISH_REQUEST_V1` の 6 envelope。詳細フィールド定義は
`references/wire-contract.md` を参照。すべて `schema_version`/`run_id`/`base_sha`/`source_set_digest`
必須（`PUBLISH_REQUEST_V1` は `run_identity` object 経由）、未知フィールド拒否、oversize 拒否、
schema repair retry 上限 1。

## Guardrails（ガードレール）

- **Allowed Paths 外を編集しない**
- `run_retrospective.py` は GitHub/Issue へのいかなる mutation も実行しない（proposal-only）
- observer/evaluator は leaf SubAgent（`tools` に `Agent`/`Skill` を含まない、nested delegation 禁止）
- evaluator は observer wave 完了・validated projection 受領前には起動しない
- raw evidence（stdout/stderr/絶対パス/credential）は `evidence_ref` 以外の形で wire envelope を通過しない

## Related（関連情報）

- `.claude/skills/agent-retrospective/scripts/collect_snapshot.py`（Child 3、#2236）
- `.claude/skills/agent-retrospective/scripts/validate_retrospective_schema.py`（Child 2、#2235/#2288）
- `.claude/agents/codebase-investigator.md` / `.claude/agents/web-researcher.md`（既存再利用）
- `docs/adr/0007-agent-retrospective-boundaries.md`
- `docs/dev/agent-skill-boundaries.md`
- `docs/dev/runtime-verification-policy.md`

## 出力制約 (OUTPUT_BUDGET_V1)

`docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` の制約に従う。routing-critical な機械可読
フィールドは削らず、人間向け説明・証跡・diff 再掲のみを削減する。
