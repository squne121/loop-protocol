# Ephemeral Wire Contract（Issue #2237 P0-2）

`.claude/skills/agent-retrospective/scripts/run_retrospective.py` が定義する 6 envelope の
フィールド定義。すべて `_WireEnvelope` mixin の strict `to_wire()`/`from_wire()` で
round-trip 検証される（未知フィールド・欠落フィールド・型不一致・oversize（256 KiB 超）・
malformed JSON はすべて `WireContractError` で fail-closed reject）。

| envelope | schema_version | dataclass |
|---|---|---|
| `SOURCE_PLAN_V1` | `source_plan/v1` | `SourcePlan` |
| `OBSERVER_RESULT_V1` | `observer_result/v1` | `EvidenceBundle` |
| `FINDING_SET_V1` | `finding_set/v1` | `FindingSet` |
| `EVALUATOR_REQUEST_V1` | `evaluator_request/v1` | `EvaluatorRequest` |
| `EVALUATION_RESULT_V1` | `evaluation_result/v1` | `Evaluation` |
| `PUBLISH_REQUEST_V1` | `publish_request/v1` | `PublishRequest` |

## SourcePlan（ソース計画）

`schema_version` / `run_id` / `base_sha` / `source_set_digest` / `sources: list[str]` /
`generated_at: str`。`prepare` phase の唯一の出力。

## EvidenceBundle（observer の出力）

`schema_version` / `run_id` / `base_sha` / `source_set_digest` / `observer_id: str` /
`evidence_ref: str` / `findings: list[dict]`。raw payload / stdout / stderr / 絶対パス /
credential を保持できるフィールドは存在しない（`private_evidence` 相当のキーは宣言されていないため、
入力に含まれていれば `unknown_field` で拒否される）。

## FindingSet（fan-in projection・所見集約）

`schema_version` / `run_id` / `base_sha` / `source_set_digest` / `observer_id: str` /
`findings: list[dict]`。`EvidenceBundle.findings` をそのまま projection したもの。

## EvaluatorRequest（評価リクエスト）

`schema_version` / `run_id` / `base_sha` / `source_set_digest` / `finding_sets: list[dict]`。
`evidence_ref` を持たない -- evaluator には schema-controlled findings のみが渡る。

## Evaluation（evaluator の出力）

`schema_version` / `run_id` / `base_sha` / `source_set_digest` /
`candidate_records: list[dict]` / `evidence_ref: str`。`finalize` phase の入力。

## PublishRequest（proposal-only・公開提案）

`schema_version` / `request_id` / `repository_id` / `target_issue: int` /
`run_identity: dict` / `candidate_records: list[dict]` /
`expected_previous_digest: str | None` / `idempotency_key: str` /
`public_projection_digest: str` / `authorization_required: bool`（常に `True`）。

**forbidden fields**（`PUBLISH_REQUEST_FORBIDDEN_FIELDS`）: `authorized` /
`authorized_by_human` / `authorization_token` / `mutation_capability`。これらは
dataclass に宣言されていないため、入力に含まれていれば汎用の `unknown_field` reject で
自動的に拒否される（専用のブロックリストロジックを別途実装する必要はない）。

## 相互検証

- `run_id` 一致検証: `validate_run_id_agreement(*envelopes)`
- schema repair retry: `parse_agent_output_with_repair(...)`（上限 `SCHEMA_REPAIR_RETRIES = 1`）

## nested smuggled-authority-field scan（不正権限フィールド検査、Issue #2237 fix_delta P0-3）

`findings: list[dict]` / `finding_sets: list[dict]` / `candidate_records: list[dict]` /
`run_identity: dict` は dataclass レベルでは `dict[str, Any]`/`list[dict]` だが、`from_wire()` は
デコード後の payload 全体を任意の深さまで再帰走査し、`SMUGGLED_AUTHORITY_KEYS`（`private_evidence`/
`authorized`/`authorized_by_human`/`authorization_token`/`mutation_capability`/`raw_stdout`/
`raw_stderr`/`raw_transcript`/`credential(s)`/`secret(s)`/`api_key`/`access_token`/`absolute_path`）
のいずれかがどの階層に現れても `smuggled_authority_field` で reject する（top-level
`additionalProperties: false` だけでは防げなかった nested smuggling への対策）。

## candidate_records（候補記録）の canonical schema 検証（Issue #2237 fix_delta P0-3/P0-4）

`Evaluation.candidate_records` / `PublishRequest.candidate_records` は、現行マージ済みの
`agent_improvement_candidate/v1`（#2288/#2289、`schemas/agent_improvement_candidate_v1.schema.json`）
を正本として `validate_retrospective_schema.validate_candidate()` で検証される。私的な shadow dialect
（`finding_identity`/`severity`/`candidate_status: open|resolved` 等）は canonical schema 不適合として
reject される。同一リスト内の `candidate_id` 重複も reject する。

## agent_retrospective_run_publication/v1（永続化 envelope、Issue #2238 Child 5）

`persist_retrospective_run.py` が `PUBLISH_REQUEST_V1` から構築し、Issue comment として投稿する envelope。
Child 2 の `agent_retrospective_run/v1` schema（`schemas/agent_retrospective_run_v1.schema.json`）とは
別の `schema_version` を持つ -- 後者が要求する per-source `source_observations[]`（acquisition window
付き）は `PUBLISH_REQUEST_V1` が保持しないため、本 envelope は repository source のみの最小
`source_observations` 単一項目で代替する（Child 2 の run identity 拡張は ADR 0007 "Remaining Parent
Gaps" の別途フォローアップ）。

フィールド: `schema_version`（`agent_retrospective_run_publication/v1` 固定）/ `repository_id` /
`target_issue` / `request_id` / `scope` / `idempotency_key`（publisher 側で再計算、caller 供給値は
信用しない）/ `expected_previous_digest` / `parent_record_digest`（optimistic concurrency の chain
link）/ `run.run_identity`（`run_id`/`base_sha`/`source_set_digest`/`generated_at`/`runtime_version`）/
`run.source_observations`（`source_type: repository` の単一項目）/ `candidate_records` / `delta_results`
（`PublishRequest` の対応 field をそのまま carry through）/ `publication_digest`（`sha256-jcs-v1`。
自分自身を preimage に含めない）。

`publication_digest` は `run_retrospective.py`'s `public_projection_digest` とは別の digest -- 前者は
「永続化された record」の binding digest（`parent_record_digest` を含む preimage）、後者は「proposal」の
binding digest。

投稿は Issue comment の marker 行（`<!-- agent_retrospective_run:v1 repository_id=... idempotency_key=... -->`）
+ fenced ```json block。post-write readback は comment ID で GET → fenced block 抽出 → canonical JSON
digest 再計算 → `publication_digest` と比較（Markdown 生 bytes 比較はしない）。
