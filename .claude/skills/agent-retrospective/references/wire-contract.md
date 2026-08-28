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

### candidate_records[] の judgment-only wire shape（Issue #2362 Scope Reframe、2026-08-28 owner 承認）

`--json-schema` として evaluator 呼び出しに渡される
`schemas/evaluation_result_v1.schema.json`（draft-07）の `candidate_records[].items` は、
evaluator-authoritative な judgment フィールドのみを要求する flat shape に絞り込まれている:

- `candidate_id` / `title` / `description`（string）
- `claim_class`（canonical `agent_improvement_candidate_v1.schema.json` の `claim_class` enum と同期）
- `subject_ref`（`kind`/`value`。canonical schema の discriminated union 制約を draft-07 で自前 author）
- `rule_id`（namespaced dot-separated token、pattern `^[a-z0-9_]+(\.[a-z0-9_]+)*$`）
- `evidence_refs[]`（`ref_type`/`source_id`/`resource_identity` のみ。`projection_digest` は含まない）

`identity` / `evaluations` / `repository_id` / `source_run_ref` / `created_at` / `updated_at` /
`candidate_status` は wire schema に存在せず、evaluator から一切要求・許容されない
（`additionalProperties: false`）。`run_retrospective.py`'s `_enrich_candidate_record()`
（deterministic-enrichment phase）が、この judgment-only 出力 + Python 側の
`repository_id`/`compute_finding_identity()`/`compute_delta()`/`PreviousStateProvider`/
実 `finding_sets` データから、canonical `agent_improvement_candidate/v1` の完全な shape
（`identity`/`evaluations[]`/`evidence_refs[].projection_digest` を含む）を 100% 決定論的に
構築する。evaluator の wire payload から `evaluations[]` を一切パースしない
（旧 PR #2367 fix_delta items 1-6 の「evaluator が出す `finding_contract` をそのまま overwrite/
passthrough する」design を上書きする、この Issue の Scope Reframe が正本）。

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

## agent_retrospective_run_publication/v1（永続化された envelope の形式。Issue #2238 Child 5 で追加）

`persist_retrospective_run.py` が `PUBLISH_REQUEST_V1` から構築し、Issue comment として投稿する envelope。
Child 2 の `agent_retrospective_run/v1` schema（`schemas/agent_retrospective_run_v1.schema.json`）とは
別の `schema_version` を持つ -- 後者が要求する publication-layer field（`idempotency_key`/
`parent_record_digest`/`publication_digest`）を持たないための区別。

フィールド: `schema_version`（`agent_retrospective_run_publication/v1` 固定）/ `repository_id` /
`target_issue` / `request_id` / `scope` / `idempotency_key`（publisher 側で再計算、caller 供給値は
信用しない）/ `expected_previous_digest` / `parent_record_digest`（optimistic concurrency の chain
link）/ `run.run_identity`（`run_id`/`base_sha`/`source_set_digest`/`generated_at`/`runtime_version`）/
`run.source_observations` / `candidate_records` / `delta_results`（`PublishRequest` の対応 field をそのまま
carry through）/ `publication_digest`（`sha256-sorted-json-v1`。自分自身を preimage に含めない）。

`publication_digest` は `run_retrospective.py`'s `public_projection_digest` とは別の digest -- 前者は
「永続化された record」の binding digest（`parent_record_digest` を含む preimage）、後者は「proposal」の
binding digest。

投稿は Issue comment の marker 行（`<!-- agent_retrospective_run:v1 repository_id=... idempotency_key=... -->`）
+ fenced ```json block。post-write readback は comment ID で GET → fenced block 抽出 → canonical JSON
digest 再計算 → `publication_digest` と比較（Markdown 生 bytes 比較はしない）。

### Issue #2238 fix_delta（OWNER 敵対的レビュー issuecomment-5381003316、P0-1〜P0-7/P1-1〜P1-4）

- **P0-5（`source_observations` の実データ化）**: `run_retrospective.py`'s `finalize()` は Child 3 の
  実際の per-collector `CollectorResult.observation` 一覧（`execute_run()`/`run_cli()` が `prepare()`
  から得る `results`）を、`PublishRequest.run_identity` dict の VALUE に追加キー
  （`source_observations`/`generated_at`/`runtime_version`）として additive に格納する。
  `PublishRequest` の dataclass field 集合自体は変更しない（`test_run_retrospective.py` がその集合を
  厳密に pin しており、この Issue の Allowed Paths 外にあるため）。`public_projection_digest` の
  preimage は元の 3-key `run_identity` サブセットのみで計算され続ける（既存 digest 比較テストへの
  影響なし）。`persist_retrospective_run.py`'s `build_run_envelope()` はこの実データを
  `run.source_observations` にそのまま永続化する（固定 placeholder は使わない）。
- **P0-1（repository_id と `--repo` の cross-check）**: `prepare_publication()`/`publish_run()` は
  最初に `publish_request["repository_id"] == repo` を検証し、不一致なら transport 呼び出しゼロで
  `RepositoryMismatch` を raise する。
- **P0-2（two-stage 公開フロー）**: `prepare_publication()`（envelope と `publication_digest` を
  file に固定）→ `authorize`（frozen file 参照の `human_authorization_receipt/v1` を発行、
  TTL <= `MAX_AUTHORIZATION_RECEIPT_TTL_SECONDS`＝10分、`approved_at` の未来日時/超過 TTL を拒否）→
  `publish_prepared()`（POST 直前に live head を再検証し、prepare 時点から変化していれば POST せず
  `StaleWriteDetected` を raise）の 3 段。CLI は `prepare-publication`/`authorize`/`publish`
  サブコマンドとして公開する。
- **P0-3（idempotency の stable digest 化）**: `compute_request_payload_digest()`（`parent_record_digest`/
  `generated_at` を含まない）で idempotency を OCC より先に評価する。`expected_previous_digest=None`
  は wildcard ではなく「head が None であること」を要求する厳格な値として扱う。
- **P0-4（fork 検出の read-time 再構成）**: `IssueCommentPreviousStateProvider.get()` は毎回
  `(repository_id, scope, parent_record_digest)` chain を全 verified record から再構成し、fork
  （同一 parent を持つ複数 child）や tip に到達しない branch を `stale` と判定する。
- **P0-6（`parse_verified_run_comment()`）**: 全読み取り経路（idempotency/OCC/provider/recovery/
  readback）が同一の検証関数を経由する: author allowlist（`trusted_publisher_logins`）/
  marker-payload cross-check / schema 形状 / public-safety re-scan / digest 再計算。自分自身の
  直近 POST の readback 検証が失敗した場合のみ `published_unverified` で停止し、index 更新を呼ばない。
- **P0-7（index wiring）**: `persist_retrospective_run.py`'s CLI に `--index-parent-issue` を追加し、
  検証済み publish 成功後に `scripts/agent-logs/update-retro-index.mjs` を実行する。
  `retro-index-builder.mjs`'s `run_digest` は envelope 自身の `publication_digest` を直接参照する
  （pretty-printed JSON の再計算 digest ではない）。
- **P1-1**: 内部識別子を `sha256-jcs-v1` から `sha256-sorted-json-v1` へ改名（実装は RFC 8785 JCS
  ではなく Python `sorted()` キー順 + compact JSON であるため）。
- **P1-2**: `create_comment_with_recovery()` は ambiguous な POST 結果が起きるたびに rescan する
  （初回のみではない）。
- **P1-4**: `IssueCommentPreviousStateProvider`'s age-based staleness はデフォルト無効
  （`stale_after_seconds=None`）。opt-in で `STALE_AFTER_SECONDS_LEGACY_DEFAULT`（7日）を指定可能。
