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

## SourcePlan

`schema_version` / `run_id` / `base_sha` / `source_set_digest` / `sources: list[str]` /
`generated_at: str`。`prepare` phase の唯一の出力。

## EvidenceBundle（observer の出力）

`schema_version` / `run_id` / `base_sha` / `source_set_digest` / `observer_id: str` /
`evidence_ref: str` / `findings: list[dict]`。raw payload / stdout / stderr / 絶対パス /
credential を保持できるフィールドは存在しない（`private_evidence` 相当のキーは宣言されていないため、
入力に含まれていれば `unknown_field` で拒否される）。

## FindingSet（fan-in projection）

`schema_version` / `run_id` / `base_sha` / `source_set_digest` / `observer_id: str` /
`findings: list[dict]`。`EvidenceBundle.findings` をそのまま projection したもの。

## EvaluatorRequest

`schema_version` / `run_id` / `base_sha` / `source_set_digest` / `finding_sets: list[dict]`。
`evidence_ref` を持たない -- evaluator には schema-controlled findings のみが渡る。

## Evaluation（evaluator の出力）

`schema_version` / `run_id` / `base_sha` / `source_set_digest` /
`candidate_records: list[dict]` / `evidence_ref: str`。`finalize` phase の入力。

## PublishRequest（proposal-only）

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
