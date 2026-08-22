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
run_retrospective.py:
  role: deterministic phase engine
  phases: [prepare, validate-observers, prepare-evaluator, finalize]
agent_tool_calls:
  owner: root Skill
  prohibited_owner: observer/evaluator leaf agents
```

`scripts/run_retrospective.py` 自身は Claude Code `Agent` tool を一切呼ばない。observer/evaluator の
起動・待機・再試行・キャンセルはすべて本 SKILL.md の手順に従う root Skill（main conversation）が行う。
詳細な設計判断・budget の根拠は `references/design-rationale.md` を参照。

## Procedure（手順）

### 1. prepare

```bash
uv run --locked python3 -c "
import sys
sys.path.insert(0, '.claude/skills/agent-retrospective/scripts')
import run_retrospective as rr
# base_sha_resolver / collectors は呼び出し元（root Skill）が用意する
# 例: base_sha_resolver = lambda: <git rev-parse main>
"
```

`run_retrospective.prepare()` を呼び、`run_id`（run-scoped nonce）と `base_sha`
（一度だけ解決、以降再解決しない）を固定した `RunContext` と `SourcePlan` を得る。
`collectors` には Child 3（`.claude/skills/agent-retrospective/scripts/collect_snapshot.py`）の
`collect_claude_code_source` / `collect_claude_gpt_source` / `collect_repository_source` /
`collect_github_source` / `collect_web_source` を束ねる。

### 2. observer wave（fan-out、`observer_parallelism: 3`）

root Skill が `Agent` tool で以下を **同時起動可** で呼び出す:

- `retrospective-runtime-observer`（Claude Code/Claude-GPT session evidence の解釈専用）
- `codebase-investigator`（既存 SubAgent の再利用。advisory、base_sha 非束縛の調査は finding authority にしない）
- `web-researcher`（既存 SubAgent の再利用。最終 evidence は Web collector で再取得・digest 化してから finding authority にする）

各呼び出しの結果（serialized JSON 文字列）を `run_retrospective.run_observer_wave()` に渡し、
`EvidenceBundle`（`OBSERVER_RESULT_V1`）へ strict validation する。失敗時は `schema_repair_retries: 1`
まで repair を試み、それでも失敗すれば **evaluator を起動せず** fail-closed で終了する（AC14）。

`DelegatedAgentPermissionPolicy`（`run_retrospective.py`）を各 Agent 呼び出しの周囲に適用し、
`git commit`/`git push`/`gh issue`/`gh pr`/filesystem write/unapproved Bash/対象 run 外 resume を拒否する。

### 3. prepare-evaluator（fan-in）

全 observer が成功した場合のみ（`partial_agent_output: reject`）、
`run_retrospective.build_finding_sets()` → `prepare_evaluator_request()` で
`EvaluatorRequest`（schema-controlled projection のみ、raw evidence を含まない）を組み立てる。

### 4. evaluator 起動（observer wave 完了後にのみ、observer と同時起動しない）

root Skill が fresh-context で `retrospective-evaluator` を `Agent` tool で 1 回起動し、
`run_retrospective.run_evaluation()` で `Evaluation`（`EVALUATION_RESULT_V1`）を strict validation する。
`evaluator_retries: 0`（再試行しない）。

### 5. delta 算出（任意、`PreviousStateProvider` 経由）

`run_retrospective.FixturePreviousStateProvider` を使い、`available`/`no_history`/`legacy_unavailable`/
`partial`/`stale` の 5 状態から `compute_delta()` で `new`/`resolved`/`recurrent`/`regressed`/`unchanged`
を算出する。production provider（実際の永続化読み取り）は #2238 の責務。

### 6. finalize

`run_retrospective.finalize()` で proposal-only `PublishRequest`（`PUBLISH_REQUEST_V1`）を生成する。
`authorized`/`authorized_by_human`/`authorization_token`/`mutation_capability` はスキーマレベルで禁止
フィールドであり、`PublishRequest` dataclass には存在しない（AC16）。人間承認・実際の Issue mutation は
別の trusted channel（`HUMAN_AUTHORIZATION_RECEIPT_V1`、#2238）が担う。

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
