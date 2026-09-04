---
title: "E2E Performance Benchmark Contract（E2E 性能ベンチマーク契約）"
status: active
related_issue: "#2159"
parent_issue: "#2119"
---

# E2E Performance Benchmark Contract

`#2119` の E2E lane split（`e2e-core` / `e2e-responsive-matrix`）が性能改善の主張（P50/P95 provider critical path shortening、gate-ready latency 非劣化）を実データで検証するための測定器契約。`#2159`（Issue A）でこの測定器自体を再設計した（reliability/failure-flaky-rate は別 Issue B のスコープ）。

## 固定 SHA ベンチマーク設計（#1064 方式）

before/after の比較は、任意の 2 コミットを事後的に比較するのではなく、事前に固定した 2 つの 40-hex commit SHA（`before_sha` / `after_sha`）に対して専用の `workflow_dispatch` benchmark route を起動し、同一条件下で複数回実行して蓄積したサンプル集合を比較する。これにより、比較対象のコミット間で無関係な変更が混入するリスクを排除する。

## サンプル identity: `workflow_run_id`

独立サンプルの単位は GitHub の `workflow_run_id` である。同一 `workflow_run_id` の rerun attempt（`run_attempt` が異なるだけの再実行）は独立サンプルとして数えない（`scripts/ci/collect_e2e_performance_benchmark.py::_dedupe_by_workflow_run_id` / `tests/ci/test_ci_performance_gate.py::_dedupe_by_workflow_run_id`）。これは `(run_id, run_attempt)` を identity にしていた旧設計の rerun 水増し問題を修正したもの。

## rerun attempt 選択ポリシー: `initial_attempt_only_v1`

同一 `workflow_run_id` に複数の rerun attempt（`run_attempt` = 1, 2, 3, ...）が存在する場合、どの attempt を canonical サンプルとして採用するかは `initial_attempt_only_v1` という単一の明示的なポリシーに固定されている（Issue #2179、PR #2172 の OWNER adversarial review issuecomment-5295659213 P1-1 の指摘への対応）。

- 採用するのは常に `run_attempt == 1`（`GITHUB_RUN_ATTEMPT` の初回値）の成功レコードのみ。
- **collector 側（`_classify_run_attempt`）: `run_attempt` フィールドが完全に欠落しているレコードは、trusted cohort から除外される（PR #2182 OWNER adversarial review issuecomment-5302446086 P0-3 での是正）。** 旧設計では欠落キーを暗黙に `run_attempt: 1` とみなして選択・manifest 出力していたため、live API で一度も検証されていない古い記録が、正規に検証済みの attempt 1 サンプルと区別できなくなる「provenance laundering」が発生していた。現在は欠落キーは `legacy_unverified_run_attempt` という専用の `evidence_errors` reason で明示的に除外され、manifest 出力に `run_attempt: 1` / `rerun_attempt_selection_policy` が合成されることはない。スキーマ的な読み取り（後方互換パース）自体は引き続き可能。
- **gate 側（`tests/ci/test_ci_performance_gate.py`）も、この collector 側の是正と同じ trust-state semantics に統一されている（Issue #2187）。** trust 判定用の `_normalize_run_attempt_trusted()` は `run_attempt` 欠落を `None`（trusted cohort から除外）として扱う一方、`_normalize_run_attempt()` は identity collision グルーピング専用ヘルパーとして維持され、欠落キーを引き続き attempt-1 スロットとして扱う（責務分離）。`_select_initial_attempt_baselines()` は `(selected, evidence_errors)` のタプルを返し、欠落キーによる除外は `legacy_unverified_run_attempt` という reason で `evidence_errors` に記録される。provider lane（`_pair_by_workflow_run_id`）だけでなく gate-ready lane（`_gate_ready_post_filter_sample_count`）にも同じ attempt selection / `workflow_run_id` dedupe が適用され、`_cli_run_details_from_pairs()` が missing/invalid な attempt から `run_attempt: 1` を合成することもない。
- **attempt 1 が失敗している、または欠落している場合、その `workflow_run_id` のサンプルは cohort から完全に除外される。** 同一 `workflow_run_id` の attempt 2 以降が成功していても、それを代わりに採用することはない（非決定論的な代替を避けるため）。除外は `evidence_errors`（`reason: missing_or_invalid_initial_attempt_excluded_from_sample` または `legacy_unverified_run_attempt`）に明示され、黙って消えることはない。運用上は、attempt 1 が失敗したサンプルを補いたい場合は新規に run を再起動して新しい `workflow_run_id` を取得する（同一 `workflow_run_id` の rerun を採用対象にはしない）。
- 選択は `dict.setdefault()` による first-seen-wins（挿入順依存）や `dict` 内包表記による実質 last-seen-wins ではなく、`workflow_run_id` ごとにレコードをグルーピングしてから attempt 1 の候補を選ぶ 2 段階の実装（`_select_initial_attempt_records` / `_select_initial_attempt_baselines`）によって、入力配列の並び順に一切依存しない。
- **identity 衝突は fail-closed（PR #2182 OWNER adversarial review issuecomment-5302446086 P1 での是正）。** identity は `(workflow_run_id, job, run_attempt)` に固定される。同一 identity を持つ複数レコードは、正規化後の内容が完全にバイト一致する場合のみ「無害な重複」として扱われ、`head_sha`・`conclusion`・`artifact_id`・`artifact_digest`・`workflow_digest`・`workflow_sha`・測定値・comparability fingerprint・選択ポリシー名のいずれか 1 つでも異なれば、その `workflow_run_id` のサンプル全体が fail-closed で cohort から除外される（旧設計は `artifact_id`/`artifact_digest` のみを比較し、それ以外のフィールドの不一致は canonical JSON の `min()` で黙って一方を採用していた）。除外は `evidence_errors`（`reason: run_attempt_identity_collision`）に記録される。`run_attempt` が欠落しているレコードは、この衝突検出のグルーピングにおいてのみ attempt 1 のスロットに含められる（trusted cohort への採用可否とは独立した扱い）ため、欠落キーのレコードと明示的な `run_attempt: 1` レコードが同一 `workflow_run_id` で内容不一致の場合も衝突として検出される。
- 採用された `run_attempt` の値と選択ポリシー識別子（`rerun_attempt_selection_policy: "initial_attempt_only_v1"`。`schemas/e2e_performance_benchmark_manifest_v1.schema.json` では既知のポリシー名のみを許容する `enum` 制約に絞られている）は、manifest の各 `RunRecord`（同スキーマの後方互換 optional field）に記録される。
- **collector 側の live-API trusted binding は厳格化されている（PR #2182 OWNER adversarial review issuecomment-5302446086 P0-2 での是正）。** `verify_run_record_against_live_api` は `run_attempt` を trusted evidence として扱い、attempt-specific jobs API（`GET .../actions/runs/{run_id}/attempts/{attempt_number}/jobs` 相当）と突き合わせて binding する。信頼される identity タプルは `(workflow_run_id, run_attempt, job_name, workflow_run_head_sha, job_conclusion, artifact_id, artifact_name, artifact_digest)` の全要素であり、旧設計のように「attempt-specific jobs API のレスポンスに何らかの job が存在する」だけでは足りない -- 対応する job の `name` が記録の `job` と一致し、その job 自身の `head_sha` が期待コミットと一致し、`conclusion == "success"` であることを要求する。さらに artifact 自体の `name` が `.github/workflows/ci.yml` の `actions/upload-artifact` 命名規約（`ci-runtime-baseline-{job}-{run_attempt}`）と一致することも検証する。これにより、attempt 2 の artifact を attempt 1 と偽って申告する relabeling 攻撃が拒否される。ライブ API 上に対応する job/attempt が存在しない、または上記いずれかの要素が不一致の場合は fail-closed で拒否する。
- **`measured_head_sha` / `workflow_run_head_sha` の分離後（#2184）、live-API の head SHA 照合は「期待コミット（target_sha 相当）との一致」ではなく「record 自身が申告する `workflow_run_head_sha` との自己整合性」のみを見る。** `verify_run_record_against_live_api` は `api_workflow_run.head_sha` / `api_job.head_sha` を `expected_head_sha`（測定対象コミット）とは比較せず、record の `workflow_run_head_sha`（既存 `merge_sha` フィールドから導出。#2184 AC2）と比較する。`measured_head_sha` と `workflow_run_head_sha` が異なること自体は live API 照合の失敗条件にしない（固定SHA benchmark dispatch では意図的に異なる値になる）。`workflow_run_head_sha` を導出できない pre-#2184 の record（`merge_sha` を持たない）に対しては、後方互換のため従来通り `expected_head_sha` との比較にフォールバックする。
- **artifact 一覧取得の pagination は一貫したページサイズを使用する（PR #2182 OWNER adversarial review issuecomment-5302446086 P0-1 での是正）。** 旧実装は 1 ページ目のみ `per_page` を省略（GitHub のデフォルト値 30 が暗黙に適用される）し、2 ページ目以降だけ `per_page=100` を指定していた。GitHub の Artifacts API のページ番号はページサイズに対して相対的なため、ページサイズが 30 から 100 に途中で変わると「2 ページ目」が指す実際のスライスがずれ、artifact 31〜100 が黙ってスキップされ得た。現在は 1 ページ目から一貫して `per_page=100` を使用し、加えて次の状態を fail-closed（`LiveAPIError`）として検出する: (a) `total_count` に到達する前に空ページに到達、(b) 複数ページに跨って同一 artifact ID が重複出現、(c) 同一フェッチ内で `total_count` が途中で変化。

## ペア化 critical path（Paired critical path）統計

post-split の `e2e-core` / `e2e-responsive-matrix` は並列実行される 2 つの独立ジョブであり、DAG 上の実際の critical path は「同一 `workflow_run_id` の中で両ジョブのうち遅い方」である。したがって provider の P50/P95 は次の式で計算する:

```
critical_path_i = max(core_duration_i, responsive_duration_i)   # 同一 workflow_run_id の i
provider_P50 = nearest_rank_v1(critical_path_1, ..., critical_path_n, percentile=50)
provider_P95 = nearest_rank_v1(critical_path_1, ..., critical_path_n, percentile=95)
```

`max(median(core), median(responsive))`（旧式）は異なる `workflow_run_id` の run を混ぜて計算するため、どの実際の 1 回の実行の wall-clock critical path も再現しない誤った統計量であり、使用しない。

pair の一方が欠損している `workflow_run_id`（例: `e2e-core` は成功したが `e2e-responsive-matrix` が cancel された）は、cohort から黙って除外せず、`evidence_errors` に明示する（`tests/ci/test_ci_performance_gate.py::_pair_by_workflow_run_id`）。

## パーセンタイル手法（percentile method）: `nearest_rank_v1`

P50/P95 の算出には 1-indexed nearest-rank 法（`rank = ceil(percentile/100 * n)`、ソート済みサンプルの `rank` 番目の値）を使う。この method 名はバージョン管理された識別子であり、`tests/ci/test_ci_performance_gate.py::_nearest_rank_percentile` と `.claude/skills/ci-test-performance/scripts/validate_ci_performance_assessment_v2.py::_nearest_rank_percentile` の両方で同一のセマンティクスを持つ（Allowed Paths の境界上、実装は複製されているが method 名とアルゴリズムは共通）。

## Gate-ready latency: 同一時計

before/after の gate-ready latency は、両アームとも GitHub API の同一時計（`workflow_run.run_started_at` を起点、対応する check run の `completed_at` を終点）から計算する（`tests/ci/test_ci_performance_gate.py::_gate_ready_latency_seconds_same_clock`）。旧設計は before 側を `measurements.jsonl` の手動合算、after 側を GitHub API という異なる時計で測定しており、apples-to-oranges 比較になっていた。

## Comparability fingerprint の三分類

`COMPARABILITY_FINGERPRINT_FIELDS` という単一 tuple による一致判定は、「cohort 内で一致すべき」「before/after 間でも一致すべき」「治療そのものとして意図的に異なる」という 3 種類の異なる意味を持つフィールドを区別できなかった。`tests/ci/test_ci_performance_gate.py` は以下の 3 分類を定義する:

- `WITHIN_COHORT_REQUIRED_EQUAL`: 同一アーム（before または after）の cohort 内の全 run で一致すべきフィールド（`host_runner_image` / `playwright_container_image_digest` / `node_version` / `pnpm_version` / `playwright_version` / `lockfile_hash` / `workflow_digest`）。
- `CROSS_COHORT_REQUIRED_EQUAL`: before/after の両アーム間でも一致すべきフィールド（インフラ・provenance が split とは無関係に変化していないことの保証。`workflow_digest` を除く上記のサブセット）。
- `INTENTIONAL_TREATMENT_DIFFERENCE`: before/after 間で意図的に異なることが期待されるフィールド（`workflow_digest` — split そのものが treatment）。

`host_runner_image`（裸の GitHub Actions runner）と `playwright_container_image_digest`（`mcr.microsoft.com/playwright@sha256:...` の固定コンテナ）は別々の provenance フィールドとして分離されている。

## Placeholder 値の拒否

fingerprint フィールドが欠損しているか、`""` / `null` / `"unknown"` / `"unknown/unknown"` / `"N/A"` のいずれかの placeholder 値を持つ run は、fail-closed で cohort から除外する（`_is_placeholder` / `_fingerprint_has_placeholder`）。2 つの placeholder 値同士が偶然一致するケースを「一致した」と誤判定しない。

## 不変 manifest（immutable manifest, 上書きしない manifest, `e2e_performance_benchmark_manifest_v1`）

`scripts/ci/collect_e2e_performance_benchmark.py` は before/after 双方の run 群から、artifact ID・artifact digest・head SHA・`workflow_run_id`・job を検証した上で `schemas/e2e_performance_benchmark_manifest_v1.schema.json` 準拠のマニフェストを生成する。このスクリプトはライブ GitHub API 呼び出しを行わない（`scripts/ci/verify_ci_check_conclusions.py` と同じ既存パターンに従い、CI ジョブまたは人間オペレータが `gh api` で事前取得した JSON を入力として受け取る）。これにより hermetic な unit test が可能になる。

各アームは `complete: true/false` を持ち、`min_run_count`（既定 20）に満たない job があれば `false` になる。manifest は書き込み後に再利用されず、再実行は新しいファイルとして生成する（in-place 上書きしない）。

## `target_sha` / `workflow_sha` / `workflow_digest` の分離（OWNER scope-authority ruling issuecomment-5299412215, item 1/P0-2）

固定 SHA ベンチマークの `workflow_dispatch` は、**必ず現在（default branch）の `ref` に対して起動する**（測定対象コミットを指す branch/tag へは向けない）。これにより `github.workflow_sha`（このワークフロー定義自身の commit）は常に最新のまま維持される。測定対象アプリケーションコードは `e2e-core` / `e2e-responsive-matrix` の `actions/checkout@v6` ステップが明示的な `ref: ${{ github.event.inputs.target_sha }}` で個別に checkout し、その結果の HEAD が `target_sha` と一致するかを検証する。

したがって manifest の各 `RunRecord` は 3 つの独立したフィールドを持つ:

- `head_sha` / `target_sha`: 測定対象アプリケーションコードの commit（`actions/checkout` の `ref:` で明示的に checkout したもの）。
- `workflow_sha`（`GITHUB_WORKFLOW_SHA` = `github.workflow_sha`）: ワークフロー定義自身の commit。ベンチマーク dispatch では意図的に `head_sha`/`target_sha` と異なりうる（新しい instrumentation ロジックで古いコードを測定するため）。
- `workflow_digest`: ワークフローファイルの内容ハッシュ（`sha256sum .github/workflows/ci.yml`）。

この 3 フィールドは決して混同・同一性検証の対象にしない（`workflow_sha != head_sha` は正常な状態であり、エラーではない）。

## `measured_head_sha` / `workflow_run_head_sha` の分離（#2184）

上記の既存 `head_sha` フィールドは、「実際に checkout・計測されたコミット」と「GitHub API 上の workflow run 自体の head SHA」という 2 つの異なる概念を単一 field に混同していた（PR #2182 OWNER adversarial review P0-4、issuecomment-5302446086）。#2184 でこの 2 概念を別 field として分離した:

- `measured_head_sha`: `e2e-core` / `e2e-responsive-matrix` の「Collect ci_runtime_baseline_v1 artifact」ステップ自身が、`target_sha` の有無に関わらず独立に実行する `git rev-parse HEAD` の出力。**`workflow_dispatch` の `target_sha` input をそのままコピーした値ではない**（`target_sha` 指定時は、既存の「Verify checked-out HEAD matches dispatched target_sha」ステップが独立にこの観測値と `target_sha` の一致を検証しており、両者が一致することは producer 側の別ステップが保証する不変条件であって、collector 側が `target_sha` を代入して合成しているわけではない）。既存 `head_sha` とは独立した別概念・別値であり、両者が常に同一であるという不変条件は主張しない（`workflow_dispatch` 経由の固定SHA benchmark 実行では、`head_sha` は `github.sha`（dispatch ref のtip）に解決されるため、一般に `measured_head_sha`（= `target_sha`）とは異なる）。
- `workflow_run_head_sha`: 既存の `ci_runtime_baseline_v1` producer が既に収集している `merge_sha`（`GH_SHA: ${{ github.sha }}`）フィールドから collector（`scripts/ci/collect_e2e_performance_benchmark.py::_derive_workflow_run_head_sha`）が導出する。producer 側に新規 env 変数・新規の `gh api` 呼び出しは追加しない。live API 照合（`verify_run_record_against_live_api`）はこの `workflow_run_head_sha` との自己整合性のみを見る（上記「rerun attempt 選択ポリシー」節末尾を参照）。

固定SHA benchmark dispatch（`workflow_dispatch` + `target_sha` 指定）では、`measured_head_sha`（= `target_sha`）と `workflow_run_head_sha`（= dispatch ref のtip）は意図的に異なる値になる。一方、通常の `pull_request` トリガー実行（`target_sha` 未指定）では `measured_head_sha` は `github.sha`（`pull_request` イベントでは synthetic merge commit を指しうる）を指すため `workflow_run_head_sha` と同値になり、PR head commit を表す `head_sha`（`github.event.pull_request.head.sha`）とは異なりうる。

`schemas/e2e_performance_benchmark_manifest_v1.schema.json` の `RunRecord` はこの分離を **optional field** として反映し、既存 `head_sha` は `required` のまま維持される。`measured_head_sha` / `workflow_run_head_sha` を持たない legacy record（既存 `head_sha` のみ）に対して、これらの新 field を `head_sha` から推測合成することはない。

`measured_head_sha` を持たず、かつ `merge_sha`（既存フィールド、#2159 以降ずっと収集されている）からも `workflow_run_head_sha` を導出できない record（= 両フィールドとも真に不在な、#2159 より前の record）は、`e2e-core` / `e2e-responsive-matrix`（`collect_e2e_performance_benchmark.py::LEGACY_AMBIGUOUS_HEAD_SHA_JOBS`）に限り、trusted cohort から明示的な `legacy_ambiguous_head_sha` reason（`evidence_errors`）で除外される（`_collect_arm`。AC4(iii)）。`merge_sha` は保持しているが `measured_head_sha` を持たない record（#2159〜#2184 の間に収集された record。`merge_sha` は #2184 以前から無条件に収集されているため、真に両フィールドとも不在なケースは #2159 より前の record に限られる）はこの除外の対象ではなく、既存の `head_sha != expected_head_sha` 検証（後方互換、変更なし）で引き続き trusted cohort に含まれ得る。この除外は `e2e-core` / `e2e-responsive-matrix` の両ジョブに限定される（分割前の `e2e` job は本 Issue の producer 変更対象外であり、過去も将来もこれらの新 field を持つことはないため、ambiguous 除外の対象にはならない）。

## `workflow_digest` / `workflow_sha` の既知の限界（#2422 に委ねる）

`workflow_digest`（`sha256sum .github/workflows/ci.yml`）は **checkout 後の** ワークフローファイルから計算されるため、`workflow_sha`（`github.workflow_sha`、workflow 定義の commit）とは別 commit 由来になり得るという既知の限界がある（hybrid `target_sha` benchmark route に起因する構造的制約）。この限界自体の解消（固定SHA benchmark route の置換）は #2184 のスコープ外であり、#2422（本 Issue に `blocked_by` される後続 Issue）に委ねる。#2184 は SHA provenance vocabulary（`measured_head_sha` / `workflow_run_head_sha`）の確立のみを行い、hybrid route 自体は置換しない。

## `host_runner_image` の provenance 限界（追加指摘 issuecomment-5299412215）

`host_runner_image` は現在も `${{ runner.os }}/${{ runner.arch }}`（例: `Linux/X64`）から生成している。GitHub 公式定義上 `runner.os`/`runner.arch` は OS 種別・CPU architecture のみを表し、hosted runner の実際の image build/version（`ImageOS`/`ImageVersion`、週次更新）を表さない。`ImageOS`/`ImageVersion` は `container:` を使うジョブ（`e2e-core`/`e2e-responsive-matrix` は両方とも `mcr.microsoft.com/playwright@sha256:...` コンテナ内で実行される）ではホスト側の環境変数がコンテナへ伝播しないため、シェルから直接取得できない（`ImageOS`/`ImageVersion` 未設定・空文字列になることを過去のセッションで確認済み）。

このため ci_runtime_baseline_v1 producer は `host_runner_image_provenance: "os_arch_fallback"` フィールドを明示的に追加し、`host_runner_image` が真の runner image version provenance ではなく OS/architecture のみの弱い代替指標であることを機械可読に記録する（比較可能性契約のフィールド名だけで「runner image provenance を満たしている」と暗黙に主張しない）。真の image version を非コンテナ化した別ジョブで取得し `needs.*.outputs` 経由で伝播する設計は、より大きな job グラフ変更を伴うため本 Issue のスコープでは実施していない（follow-up 候補）。

## `CI_TEST_PERFORMANCE_ASSESSMENT_V2` の percentile 再計算（AC8）

`validate_ci_performance_assessment_v2.py` は、`runtime_delta.<cohort>.run_details[].duration_seconds` の raw sample が存在する場合、それらから `nearest_rank_v1` で P50/P95 を再計算し、cohort の自己申告 `p50_seconds` / `p95_seconds` と照合する（`percentile_recomputation_mismatch_p50` / `percentile_recomputation_mismatch_p95`）。`run_details` は `schemas/ci_runtime_delta_v2.schema.json` 上 optional であり、存在しない場合はこのチェックをスキップする（後方互換）。

## `CI_TEST_PERFORMANCE_ASSESSMENT_V2` producer の配線（結線, wiring, AC10）

`.github/workflows/ci.yml` の `codex-execpolicy` job に `Generate CI_TEST_PERFORMANCE_ASSESSMENT_V2 artifact` / `Validate CI_TEST_PERFORMANCE_ASSESSMENT_V2 artifact` / `Upload CI_TEST_PERFORMANCE_ASSESSMENT_V2 artifact` の 3 ステップを追加した。この producer は毎回の CI 実行で `claim.kind: none` の assessment を実際に生成・検証・artifact 化する（`if-no-files-found: error`）。固定 before/after SHA ベンチマークによる実際の性能主張（`claim.kind: improvement` 等）は、この毎回実行される producer とは別に、`scripts/ci/collect_e2e_performance_benchmark.py` の専用 benchmark route から out-of-band に作成する。

### 実 production gate の配線（OWNER scope-authority ruling issuecomment-5299412215, items 2/P0-8・3/P1-3/AC11）

上記の毎回実行される producer とは別に、`.github/workflows/ci.yml` の `e2e-performance-benchmark-assessment-gate` steps (inside the existing `codex-execpolicy` job)（`workflow_dispatch` の `run_performance_assessment_gate: true` で opt-in、通常の push/pull_request では絶対に実行されない）が、`tests/ci/test_ci_performance_gate.py` を実 CLI として呼び出す:

```bash
uv run --locked python3 tests/ci/test_ci_performance_gate.py \
  --cohort-fixture <cohort_fixture.json> \
  --output <gate_result.json>
```

このエントリポイント（`run_evidence_gate` / `_cli_main`）は 2 つの機構を 1 つの実行可能パスへ配線する:

1. **AC11 hard-check の実配線**: `_evidence_readiness_hard_check_post_filter`（post-filter サンプル数を before/after 両アームで再検証）を before/after それぞれに対して呼び出す。いずれかのアームが `MIN_COHORT_RUN_COUNT`（20）未満なら `EvidenceInsufficientError` を捕捉し、`gate_status: insufficient_evidence` を書き出した上で **非 zero exit（1）** で終了する。単体テストの中だけで存在する関数ではなく、実 workflow job の実行結果として非 zero exit が観測される。
2. **P0-8 real producer の実配線**: 両アームが 20 件以上を満たした場合のみ `build_assessment_from_percentile_cohorts` を呼び出し、`claim.kind != none` の実 assessment を計算し、続けて `validate_ci_performance_assessment_v2.py` の構造/意味検証を通す。検証に失敗した場合も非 zero exit（2）とし、fail-open にしない。

`cohort_fixture_path` を指定しない場合、このジョブは `MIN_COHORT_RUN_COUNT` を意図的に下回る smoke fixture（サンプル数 3）を使って自身を実行する。これは「実 20-run history がこの実装セッションには存在しない」という OWNER が明示的に許容した状態（"20件未満なら fail-closed する production path を先に配線できます"）を、実際の workflow job 実行として証明するためであり、20-run 蓄積そのものは #2155 のスコープのまま変わらない。

## Runtime Verification Applicability

Issue #2159 の `decision: immediate` の下、cohort 依存（実 GitHub Actions 20-run history が必要）な統合テストは、本実装セッションのようにライブ history が存在しない環境では `pytest.skip()`（`tests/ci/test_ci_performance_gate.py` の既存 3 テスト）で SKIP する。これは `docs/dev/runtime-verification-policy.md` の SKIP 規約に従うものであり、fabricated PASS ではない。cohort 非依存のロジック（fingerprint 分類、paired critical path 計算、同一時計 latency、percentile 再計算、collector の artifact 検証）は fixture-driven unit test で実挙動を検証済みである。

close-verification（最終ゲート）用途には `EvidenceInsufficientError` / `_evidence_readiness_hard_check` の非 zero exit 経路を使う（AC11）。`pytest.skip()` を close 条件に使用しない。
