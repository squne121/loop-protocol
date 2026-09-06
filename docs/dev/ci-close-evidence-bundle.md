---
title: close-grade 証跡束ねバンドル（close-evidence/）
status: draft
related_issue: "#2486"
related_parent_issue: "#2119"
---

# close-grade 証跡束ねバンドル（close-evidence/）

`scripts/ci/build_close_evidence_bundle_v1.py`（producer）と
`scripts/ci/validate_close_evidence_bundle_v1.py`（validator）は、
#2423 の Performance close-grade receipt（`CI_PERFORMANCE_CLOSE_GRADE_RESULT_V1`）と
#2424 の Reliability close-grade receipt（`CI_RELIABILITY_CLOSE_GRADE_RESULT_V1`）を、
1 つの standalone-verifiable な `close-evidence/` bundle directory に束ねる薄い
producer/validator である。#2423/#2424 の統計計算・cohort materialization・
eligibility ロジック自体は一切再実装せず、両者の実出力をそのまま consume する。

`#2155` が実 CI evidence に基づく close 判断（AC6/AC7）を行う際の入力として、この
bundle を readback/検証できる形にすることが目的。本 Issue（#2486）は producer/validator
の実装と publication receipt schema の定義のみを扱う。実際の呼び出し・実行担当・
publication receipt の生成/投稿は `#2155` の scope。

## 呼び出し方

### producer（生成コマンド）

```bash
uv run --locked python3 scripts/ci/build_close_evidence_bundle_v1.py \
  --performance-receipt <path to CI_PERFORMANCE_CLOSE_GRADE_RESULT_V1 file> \
  --reliability-receipt <path to CI_RELIABILITY_CLOSE_GRADE_RESULT_V1 file> \
  --experiment-manifest <path to e2e_performance_benchmark_manifest_v2 file> \
  --output-dir close-evidence
```

両 receipt が close-grade eligible（下記「Close-grade success condition」参照）である
場合のみ `close-evidence/` を生成する。いずれかが不合格の場合は non-zero exit で
fail-closed になり、bundle directory は一切生成されない（部分生成もしない）。

### validator（検証コマンド）

```bash
uv run --locked python3 scripts/ci/validate_close_evidence_bundle_v1.py --bundle-dir close-evidence
```

`--bundle-dir` のみを入力とし、bundle directory を元の生成場所から別の場所へ
移動した後でも成功する（`inputs/` 配下のファイルを bundle directory から読み直す
ため、元の作業ディレクトリや pytest fixture 変数には依存しない）。

## Bundle directory 構造

```
close-evidence/
  close_evidence.json
  inputs/
    experiment-manifest.json
    performance-close-grade-result.json
    ci_reliability_close_grade_result_v1.json
```

`inputs/` 配下は producer に渡した元ファイルの byte-identical コピーである。

## Close-grade 合格条件（producer/validator 共通の判定条件）

- **Performance**: `performance_assessment.complete == true` かつ
  `validation.semantic_valid == true` かつ `validation.approval_eligible == true` かつ
  トップレベル `exit_code == 0`。
- **Reliability**: `aggregate.complete == true` かつ `aggregate.semantic_valid == true` かつ
  `aggregate.sample_satisfied == true` かつ `aggregate.all_non_inferior == true` かつ
  `aggregate.exit_code == 0`。加えて `workflow_failure_rate` /
  `playwright_flaky_test_rate` / `playwright_terminal_failure_rate` の 3 metric と
  対応する `validator_results` エントリが過不足なく存在すること。

いずれかが不合格の場合、producer は bundle を生成せず non-zero exit で終了する。
validator も同じ条件を `inputs/` 配下のコピーから再検証する（`close_evidence.json`
の存在自体を close-grade eligible の証明として信用しない）。

## `close_evidence.json` の schema 定義（`CI_CLOSE_EVIDENCE_BUNDLE_V1`）

| field | 型 | 説明 |
| --- | --- | --- |
| `schema` | string | 固定値 `CI_CLOSE_EVIDENCE_BUNDLE_V1` |
| `schema_version` | int | 固定値 `1` |
| `experiment_identity` | string | performance receipt の `experiment_identity` をそのまま記録 |
| `tested_workflow_sha` | string | experiment manifest 自身の `workflow_sha` に束縛（別の provenance SHA で代用しない） |
| `workflow_run_ids` | `{monolith: [int...], split: [int...]}` | layout 別・root run set（flatten しない） |
| `experiment_manifest_file_sha256` | `sha256:<hex>` | `inputs/experiment-manifest.json` の生バイト SHA-256 |
| `experiment_manifest_canonical_digest` | `sha256:<hex>` | reliability receipt の `manifest_digest`（正規化 JSON digest）を verbatim 記録 |
| `performance_close_grade_result_file_sha256` | `sha256:<hex>` | `inputs/performance-close-grade-result.json` の生バイト SHA-256（コピー整合性） |
| `reliability_close_grade_result_file_sha256` | `sha256:<hex>` | `inputs/ci_reliability_close_grade_result_v1.json` の生バイト SHA-256（コピー整合性） |
| `reliability_canonical_output_digest` | `sha256:<hex>` | reliability receipt の self-excluding `canonical_output_digest` を verbatim 記録 |
| `performance_run_set_digest` | `sha256:<hex>` | performance receipt の `run_set_digest` を verbatim 記録（参考情報） |
| `reliability_receipt_run_set_digest` | `sha256:<hex>` | reliability receipt の `receipt_run_set_digest` を verbatim 記録（参考情報） |
| `bundle_payload_digest` | `sha256:<hex>` | 上記フィールドすべて（このフィールド自身は除く）を canonical JSON 化して SHA-256 した self-excluding digest |

## Digest semantics（明確に区別された digest algorithm）

| digest field | 対象 | algorithm |
| --- | --- | --- |
| `experiment_manifest_file_sha256` | `inputs/experiment-manifest.json` の生バイト | 生バイト SHA-256（performance receipt の `manifest_sha256` と同一 algorithm、独立に再計算し一致検証） |
| `experiment_manifest_canonical_digest` | reliability receipt の `manifest_digest` | 正規化 JSON（`sort_keys=True`, compact separators）SHA-256（#2424 owner algorithm、verbatim コピー） |
| `performance_close_grade_result_file_sha256` / `reliability_close_grade_result_file_sha256` | `inputs/` 配下の各コピーファイルの生バイト | 生バイト SHA-256（コピー整合性のみ。receipt 内部の digest フィールドとは無関係） |
| `reliability_canonical_output_digest` | reliability receipt の `canonical_output_digest` | #2424 owner の self-excluding 正規化 JSON SHA-256（verbatim コピー） |
| `bundle_payload_digest` | `close_evidence.json` 自身（自フィールド除く） | self-excluding 正規化 JSON SHA-256（#2424 の `canonical_output_digest` と同じパターン） |

`experiment_manifest_file_sha256`（生バイト）と `experiment_manifest_canonical_digest`
（正規化 JSON）は別の digest algorithm であり、値を直接比較しない。

## Run-set binding（arm 単位、flatten しない）

`workflow_run_ids` は performance receipt の `arms.<layout>.workflow_run_ids`
（文字列表現）と reliability receipt の `canonical_workflow_run_ids.<layout>`
（整数表現）を、比較用に型正規化した上で layout ごとに独立して membership 一致を
検証する。`performance_eligible_workflow_run_ids`（metric-specific projection）は
root run-set binding の代用として使わない。

## Artifact semantics（成果物 identity）と publication receipt の分離

`close_evidence.json` は GitHub Actions artifact 自身の ID・digest・URL を含まない
（upload 前に生成されるため、循環依存を作らないため）。これらの upload 後情報は
`CI_CLOSE_EVIDENCE_PUBLICATION_RECEIPT_V1`（`build_publication_receipt()`）という
別の小さな schema に分離する。

| field | 説明 |
| --- | --- |
| `schema` | 固定値 `CI_CLOSE_EVIDENCE_PUBLICATION_RECEIPT_V1` |
| `schema_version` | 固定値 `1` |
| `experiment_identity` | 対応する `close_evidence.json` の `experiment_identity` |
| `bundle_payload_digest` | 対応する `close_evidence.json` の `bundle_payload_digest`（bundle とのバインド） |
| `github_artifact_id` | GitHub Actions artifact ID |
| `github_artifact_digest` | GitHub Actions artifact digest |
| `artifact_url` | artifact の URL |

実際の値の記入・投稿は `#2155` の scope。

## Out of Scope（本 Issue では扱わない）

- `#2423`/`#2424` の統計計算・cohort materialization・eligibility ロジック自体の変更。
- `#2155` の実 CI dispatch・実 run readback・publication receipt の実際の生成/投稿・
  AC6/AC7 の close 判断自体。
- monolith の `missing_pair_e2e-responsive-matrix` performance 適格性問題の修正
  （`#2422`/`#2423` owner scope）。
