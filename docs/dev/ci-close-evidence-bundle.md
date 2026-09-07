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
uv run --locked python3 scripts/ci/build_close_evidence_bundle_v1.py build \
  --performance-receipt <path to CI_PERFORMANCE_CLOSE_GRADE_RESULT_V1 file> \
  --reliability-receipt <path to CI_RELIABILITY_CLOSE_GRADE_RESULT_V1 file> \
  --experiment-manifest <path to e2e_performance_benchmark_manifest_v2 file> \
  --output-dir close-evidence
```

`build` サブコマンド（Issue #2555 で追加。producer/validator の中核ロジック自体は
変更していない、CLI のサブコマンド化のみ）。

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

Reliability については、`validator_results` のキー集合が 3 metric と過不足なく
一致するだけでなく、`assessment_content_digests` にも同じ 3 metric が過不足なく
存在し、各 digest が `sha256:<64 桁 hex>` 形式であること、各 `validator_results`
エントリが object であること（`null` 不可）を要求する。加えて、`aggregate.*` が
成功を宣言している receipt では、個別の `validator_results.<metric>.exit_code` /
`structural_valid` / `semantic_valid` のいずれかが aggregate の成功と矛盾しては
ならない（PR #2528 review fix_delta: `assessment_content_digests` を一切検査せず、
`validator_results` の中身も確認していなかった欠落の修正）。

## Input cross-binding（3 入力間の相互整合性、PR #2528 review fix_delta）

Performance receipt・Reliability receipt・experiment manifest がそれぞれ個別に
close-grade eligible であることは、3 者が **同じ実験の評価結果** であることを
保証しない。producer/validator 共通の `verify_input_cross_binding()` が、bundle
生成・検証の前提として以下を追加で検証する（いずれか 1 つでも不一致なら
fail-closed）。

- `manifest.experiment_identity == performance.experiment_identity ==
  reliability.experiment_identity`（3 者すべて一致。2 者だけの比較では代替しない）。
- `sha256_of_canonical_json(manifest) == reliability.manifest_digest`
  （#2424 owner の `sha256_of_canonical_json()` を再利用。新しい digest algorithm
  は追加しない）。
- reliability receipt 自身の self-excluding `canonical_output_digest` が
  内部的に自己整合していること（`canonical_output_digest` フィールド自身を
  除いた内容を再度 canonical JSON 化して一致することを確認 -- #2424 の
  `build_canonical_output()` と同じ自己除外パターン）。
- `performance.run_set_digest == reliability.receipt_run_set_digest`
  （#2424 が performance receipt の値をそのままコピーしている、同一由来の値）。
  一方、manifest の `experiment_run_set_digest` と performance の
  `run_set_digest` は別 owner algorithm であり、比較しない。

## `close_evidence.json` 宣言値の再導出照合（PR #2528 review fix_delta）

`close_evidence.json` 自身が同梱 `inputs/` と一致した raw-byte digest / run-set
binding を持つことだけでは、宣言された `experiment_identity` や各 digest フィールドが
改ざんされていないことは証明できない（外側の `bundle_payload_digest` を正しく
再計算し直せば、これらの宣言値は自由に書き換えられてしまうため）。

そこで validator は、`compute_declared_core_fields()`（producer が `close_evidence.json`
を組み立てる際に使う関数と同一）を `inputs/` 配下のコピーに対して呼び出し、
以下 7 フィールドについて再導出した期待値と、`close_evidence.json` 自身が宣言する
値を個別に比較する。1 つでも不一致、または欠落していれば fail-closed になる。

- `schema`（固定値 `CI_CLOSE_EVIDENCE_BUNDLE_V1`）
- `schema_version`（固定値 `1`）
- `experiment_identity`
- `experiment_manifest_canonical_digest`
- `reliability_canonical_output_digest`
- `performance_run_set_digest`
- `reliability_receipt_run_set_digest`

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

`monolith`/`split` の各 layout は、performance receipt の `arms.<layout>` と
reliability receipt の `canonical_workflow_run_ids.<layout>` の両方に必須である。
どちらか一方でも layout 自体が欠落している場合は、`or {}` / `or []` で空集合に
丸めて「両者とも空集合で一致」と判定してはならず、構造的な欠陥として fail-closed
になる（PR #2528 review fix_delta: 両 receipt の run-set オブジェクトを空にした
場合に空集合同士の一致として受理していた欠陥の修正）。

`close_evidence.json` 自身が宣言する `workflow_run_ids` についても、重複検出
（`find_duplicate_run_ids()`）を **集合化する前に** 適用する（PR #2528 review
fix_delta: 先に `normalize_run_ids()` で集合化してから比較していたため、bundle 側
自身の重複 run ID が黙って吸収されていた欠陥の修正）。

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

実際の値の記入・投稿は `#2555` の scope（`publication-receipt` サブコマンド、下記）。

## 手動 workflow_dispatch 配線（`close-evidence-publication` job、Issue #2555）

`.github/workflows/ci.yml` に `workflow_dispatch` トリガーの手動 opt-in job
`close-evidence-publication` が追加されている。operator は
`close_evidence_source_artifact_id`（`reliability-assessment` job が upload した
`ci-reliability-close-grade-result-*` artifact の GitHub Actions artifact ID）
1 個のみを指定して dispatch する。空値の場合、job-level `if:` gate
（`github.event.inputs.close_evidence_source_artifact_id != ''`）により
job 自体が起動しない（push/pull_request イベントでも同様に起動しない）。

job は次の順に実行する:

1. `close_evidence_source_artifact_id` で指定された artifact を
   `gh api repos/${{ github.repository }}/actions/artifacts/{id}` /
   `.../zip` でダウンロードし、`reliability-assessment-input/manifest.json` /
   `reliability-assessment-input/receipt.json` /
   `reliability-assessment-output/ci_reliability_close_grade_result_v1.json`
   を固定パスから取得する。
2. `build_close_evidence_bundle_v1.py build` を実行し `close-evidence/` を生成する。
3. `validate_close_evidence_bundle_v1.py` を実行し PASS した場合のみ後続へ進む。
4. `close-evidence/` ディレクトリ全体を `actions/upload-artifact@v7`
   （`if-no-files-found: error`）で artifact 名 `close-evidence-bundle-v1`
   として upload する（artifact A）。
5. artifact A の upload step の action outputs（`artifact-id` / `artifact-url` /
   `artifact-digest`）を **値を加工せず verbatim** で
   `scripts/ci/build_close_evidence_bundle_v1.py publication-receipt` へ渡し、
   `build_publication_receipt()` を呼び出して
   `close-evidence-publication-receipt-v1.json` を生成する。

   ```bash
   uv run --locked python3 scripts/ci/build_close_evidence_bundle_v1.py publication-receipt \
     --close-evidence-json close-evidence/close_evidence.json \
     --github-artifact-id <artifact A upload outputs.artifact-id> \
     --github-artifact-digest <artifact A upload outputs.artifact-digest> \
     --artifact-url <artifact A upload outputs.artifact-url> \
     --output close-evidence-publication-receipt-v1.json
   ```

6. 生成した `close-evidence-publication-receipt-v1.json` を artifact A とは
   別の artifact 名 `close-evidence-publication-receipt-v1` として
   `actions/upload-artifact@v7`（`if-no-files-found: error`）で upload する
   （artifact B）。

job-level `permissions` は `contents: read` に加え `actions: read`
（cross-run Reliability artifact readback 用途に限定。write 権限・OIDC・
attestation・署名・外部 credential は追加しない）。

### AC8 動作検証（bounded smoke run）

`scripts/ci/dispatch_close_evidence_publication_smoke_v1.py`
（`scripts/ci/tests/test_dispatch_close_evidence_publication_smoke_v1.py`
経由で `uv run pytest` から起動）が、既存の実在する Reliability close-grade
artifact ID を自動解決した上で `close-evidence-publication` job を実際に
`workflow_dispatch` し、run 完了を poll し、conclusion が success であること、
artifact A/B の両方が生成されたことを検証する。`GH_TOKEN`/`GITHUB_TOKEN` に
`actions: read` scope が無い場合、または smoke に使用可能な既存 Reliability
artifact ID をリポジトリ内から解決できない場合（または現在の worktree の
HEAD が origin へ push 済みでない場合）は SKIP（pytest.skip()、exit 相当 77）
を返す。SKIP は PASS の代替ではない。

## 失敗時の書き込み順序（PR #2528 review fix_delta）

producer は、run ID の `int()` 変換を含む全ての解析・正規化を `inputs/` への
ファイル書き込みより **前** に完了させる。不正な run ID 表現（整数へ変換できない
文字列など）は、他の close-grade eligibility / binding / cross-binding チェックと
同様に、書き込み前に `CloseEvidenceBundleError` として fail-closed になり、
`inputs/` を含む bundle directory は一切作成されない（部分生成を残さない）。

## Out of Scope（本 Issue では扱わない）

- `#2423`/`#2424` の統計計算・cohort materialization・eligibility ロジック自体の変更。
- `#2155` の AC6/AC7 close 判断自体（本 bundle・publication receipt を証跡として
  参照するのみで、close 可否の判定ロジックは `#2155` の scope）。
- 44-run 本番実験の実行そのもの、自動トリガー（push/PR 時の自動実行）化
  （`#2555` は手動 opt-in `workflow_dispatch` route の追加のみ）。
- monolith の `missing_pair_e2e-responsive-matrix` performance 適格性問題の修正
  （`#2422`/`#2423` owner scope）。
