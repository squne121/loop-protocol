# Ephemeral wire contract（plugin distribution 版）

`skills/run/scripts/run_retrospective.py` が定義する 6 envelope の要約。詳細フィールド定義は
各 dataclass（`SourcePlan`/`EvidenceBundle`/`FindingSet`/`EvaluatorRequest`/`Evaluation`/
`PublishRequest`）の docstring を正本とする。

| envelope | schema_version | 生成フェーズ | 消費者 |
|---|---|---|---|
| `SOURCE_PLAN_V1` | `source_plan/v1` | `prepare` | observer prompt binding |
| `OBSERVER_RESULT_V1` | `observer_result/v1` | observer wave（各 observer Agent） | `build_finding_sets` |
| `FINDING_SET_V1` | `finding_set/v1` | `build_finding_sets`（fan-in） | `prepare_evaluator_request` |
| `EVALUATOR_REQUEST_V1` | `evaluator_request/v1` | `prepare_evaluator_request` | evaluator Agent |
| `EVALUATION_RESULT_V1` | `evaluation_result/v1` | evaluator Agent（judgment-only） | `run_evaluation`（deterministic enrichment） |
| `PUBLISH_REQUEST_V1` | `publish_request/v1` | `finalize` | 呼び出し元（proposal-only、mutation なし） |

すべて `schema_version`/`run_id`/`base_sha`/`source_set_digest` 必須（`PUBLISH_REQUEST_V1` は
`run_identity` object 経由）、未知フィールド拒否、oversize（262,144 bytes）拒否、schema repair
retry 上限 1（`SCHEMA_REPAIR_RETRIES`）。`SMUGGLED_AUTHORITY_KEYS`（`private_evidence`/
`authorized`/`authorization_token`/`raw_stdout`/`credential` 等）は任意のネスト深さで拒否される。

project Skill 版（`.claude/skills/agent-retrospective/`）との違いは、本 plugin 版が
`codebase-investigator` の AGY role-adapter（native `CODEBASE_INVESTIGATION_RESULT_V1` schema への
切り替え）と Latitude runtime evidence enrichment を実装しないことである -- 本 plugin の
`codebase-investigator`/`web-researcher` は常に `OBSERVER_RESULT_V1` を直接話す軽量な独立実装。
