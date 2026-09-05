---
adr_id: "0009"
title: "Visual Impact Evidence Producer: Same-Run Producer Authenticity (Fixed Producer / Attestation Adoption Decision)"
summary_ja: "visual-impact-policy evidence producerに対するsame-run producer authenticity（同一workflow run内でのproducer偽装）への対策として、固定producer・full-SHA pinned reusable workflow・artifact attestationの採用可否を検討し、個人開発規模での費用対効果を踏まえてDEFER（現状維持）と決定した記録。"
status: accepted
decision_date: "2026-09-05"
confirmed_date: null
related_issues:
  - "#2101"
  - "#2093"
  - "#2091"
  - "#2229"
  - "#2379"
  - "#2388"
  - "#2230"
supersedes: []
superseded_by: null
---

# ADR 0009: Visual Impact Evidence Producer — Same-Run Producer Authenticity

<!-- 注記: 本ファイル名は Issue #2101 の Allowed Paths (`docs/adr/0007-visual-impact-immutable-evidence-producer.md`) の指定通りだが、`docs/adr/0007-agent-retrospective-boundaries.md` が既に adr_id "0007" を使用しているため、本ADRの adr_id は次の空き番号 "0009" を採番した。ファイル名の "0007" プレフィックスと adr_id "0009" の不一致は、本Issueのスコープ外のfollow-upリネーム候補として `## Notes` に記録する。 -->

## Context（背景）

親Issue #2093（visual-impact-policy-trusted の残存検証ギャップ縮小）の子3として、`visual-impact-policy` evidence producer/consumer/verifier について、同一 workflow run 内での candidate-controlled producer 偽装（same-run producer authenticity）に対して、固定 producer・full-SHA pinned reusable workflow・artifact attestation を採用するかどうかを決定する。

`docs/dev/visual-impact.schema.json` が定義する `VISUAL_IMPACT_DECISION_V1` は、VRT（Visual Regression Test）の合否判定を PR head の申告に依存せず検証するための evidence manifest である。producer（`.github/workflows/ci.yml` の `visual-impact-policy` job）、trusted consumer（`.github/workflows/visual-impact-trusted-consumer.yml`）、verifier（`scripts/agent-ops/resolve_visual_impact.py`）の3層構成である。

## Current Guarantee Boundary（現状の保証境界）

現行 main（HEAD `8d405a44`）の fact-check結果（codebase-investigator, PR #2229/#2379/#2388反映済み）:

| 境界 | 保証状況 | 根拠 |
| --- | --- | --- |
| 別 run・別 attempt の CheckRun へのすり替え | **防止済み** | `scripts/agent-ops/resolve_visual_impact.py` の `verify_component_vrt_checkrun_provenance()`（PR #2229）が `expected_workflow_run_id` / `expected_run_attempt` の exact match を fail-closed で強制する |
| artifact 取得の run/attempt 整合性 | **防止済み** | `visual-impact-trusted-consumer.yml` が attempt-specific artifact 名（`visual-impact-decision-v1-${RUN_ATTEMPT}` 等）で取得し、`resolve_visual_impact.py --mode acquire-trusted-artifact` が `--expected-artifact-head-sha` を照合する |
| V3 manifest の identity 整合性 | **防止済み** | `docs/dev/visual-impact.schema.json` の `VISUAL_IMPACT_DECISION_V1` は `workflow_run_id` / `run_attempt` を required とし（Issue #2230 AC2）、producer 自身の `${{ github.run_id }}` / `${{ github.run_attempt }}` を content に束縛する。rerun 間の stale artifact 誤選択を防止（PR #2379/#2388） |
| **同一 run 内の producer 自体の偽装（same-run producer authenticity）** | **未保証** | `resolve_visual_impact.py` 内コメント（`verify_component_vrt_checkrun_provenance()` docstring）が明示: 「This deliberately verifies only cross-run/attempt substitution; a candidate-controlled producer inside the same run remains #2101's attestation boundary.」 |

補足: `visual-impact-policy` job（`ci.yml` 内、`needs: [component-vrt-report]`）は `pull_request` トリガーの workflow の一部であり、job定義自体は PR head 由来である。既に「trusted evaluator の base_sha からの materialize」（`git show "${BASE_SHA}:scripts/agent-ops/resolve_visual_impact.py"` 等、Issue #2019 P0-6）により評価ロジック自体の改変余地は縮小済みだが、job のステップ列自体（CheckRun API呼び出し・成功/失敗判定の呼び出し順）は PR head の `ci.yml` が定義する。trusted consumer（`workflow_run` トリガー、base branch の workflow 定義を使用）は独立再導出（`git show`/`git diff` によるstatic data read のみ、candidate PR headのコードは一切checkout・実行しない）でこの job定義自体を信頼せず、base_sha/PR body/changed-paths/registryのハッシュ照合とCheckRun exact-ID/run-attempt照合のみを行う。これが未保証範囲の技術的な形状である。

`.github/workflows/ci.yml` の `visual-impact-policy` job（および component-vrt-report job）は現在いずれも job-level reusable workflow 呼び出し（`jobs.<job_id>.uses: owner/repo/path@sha`）を使用しておらず、`actions/checkout@v6` 等の step-level Action 呼び出しのみである。したがって「full-SHA pinned reusable workflow化」は既存reusable参照のSHA置換ではなく、`ci.yml` からのproducer job切り出しを意味する。

`.github/workflows/visual-impact-consumer.yml` および `.github/workflows/visual-impact-policy.yml` は過去(`3559ad21`)・現在(`8d405a44`)いずれのcommitにも存在しない（過去のIssue本文の誤記であり、OWNER指摘・fact-check済み）。

`visual-impact-policy` は現時点で GitHub 側の branch protection / ruleset のいずれにも required check として登録されていない（`docs/ops/branch-protection.md` の drift記録）。一方、このリポジトリ自身のCI判定器（`.claude/skills/pr-review-judge/scripts/ci_verdict_summary_v2.py` の `CLASSIFICATION_MAP`）は `("ci", "visual-impact-policy"): "required"` として分類しており、`classification == "unknown"` は `determine_check_verdict()` が常に blocking（`gh_error`）として扱う。したがって GitHub側のrequired登録有無に関わらず、このリポジトリのPRレビュー経路では実質的にgatingされている。

## Considered Options（検討した選択肢）

### Option 1: 現行の独立再導出 + CheckRun/run-attempt/V3整合性を維持する（same-run producer authenticityは未保証のまま明示）

追加実装なし。既存の3層防御（cross-run/attempt CheckRun substitution保護、artifact run/attempt整合性、V3 manifest identity整合性）を維持し、same-run producer authenticityは技術的に防止されていないことをドキュメントで明示する。

### Option 2: reusable workflowのfull-SHA pinningのみを採用する

`visual-impact-policy` job（および必要ならcomponent-vrt-report job）を `{owner}/{repo}/.github/workflows/visual-impact-producer.yml@<40桁SHA>` 形式のreusable workflow呼び出しに切り出し、固定SHAで参照する。

GitHub公式ドキュメントの確認結果（web-researcher調査）: full-SHA pinningはworkflow定義自体（そのSHAにあるコード）を不変にするが、reusable workflow呼び出し元（caller、すなわちPR head側の`ci.yml`）が`actions/checkout`を実行した場合、デフォルトでは呼び出し元リポジトリ（PR head）がcheckoutされる。reusable workflow自身のコードをcheckoutするには`job.workflow_repository`/`job.workflow_sha`コンテキストの明示的な利用が必要。したがって、full-SHA pinningは「呼ばれるworkflow定義自体の改変不可性」は保証するが、「そのworkflowが評価対象として読み込むデータ（component-vrt-report の生成物、PR head由来のVRTスクリーンショット等）」の真正性は別問題であり、pinningだけでは"candidateがpinned producerを呼ばずに同名artifactを自前生成して成功を偽装する"経路を塞げない。

### Option 3: 固定producer + artifact attestation + trusted側でのsigner identity/consumed evidence/execution identity厳密照合を採用する

`visual-impact-policy` producerを独立したfull-SHA pinned reusable workflowとして切り出し、GitHub artifact attestation（`actions/attest`）でevidence manifestに署名し、`visual-impact-policy-trusted`側で `gh attestation verify --signer-workflow --signer-digest` によりsigner identityを検証する。

GitHub公式ドキュメントの確認結果: attestationのpredicateはworkflow参照・commit SHA・triggering eventへのリンクを提供するが、attestation自体は「証拠生成処理（VRT比較ロジック）が正しく実行されたこと」までは保証しない（"Generating attestations alone doesn't provide any security benefit; the attestations must be verified"、predicateの内容自体はworkflow実行結果の自己申告でありcryptographicに保護されるのはcertificateとtimestampのみ）。reusable workflowでattestationを生成するにはcaller・callee双方に`attestations: write`, `id-token: write`, `contents: read`権限が必要（caller側、すなわちPR head由来のpull_requestトリガーworkflowにこれらの書き込み権限を付与することはpermission-scope上のリスク増加を伴う）。

## Decision
Decision: DEFER
## Rationale
- 個人趣味開発プロジェクトであり、`same-run producer authenticity` の脅威モデルは「リポジトリへのPR作成権限を持つ攻撃者が、自分のPR内でVRT評価ロジックの実行結果を偽装する」という限定的なものである。現状、最終マージ判断は常に人間（OWNER）が行っており、`visual-impact-policy` はGitHub側のbranch protection/rulesetでもまだrequired checkとして登録されていない（`docs/ops/branch-protection.md` 記載のdrift）。したがって、同一run内producer偽装が成立した場合の実害は「人間レビューを経ずに直接mergeされる」ことではなく、「1つのCI signalが誤った成功を報告する」ことに留まる。
- Option 3（固定producer + attestation）は、web-researcher調査が示す通り、攻撃者が実際に偽装できるのは「pinned producerが読み込む評価対象データ（VRT差分そのもの）」であり、workflow定義のSHA固定・attestation署名はこの経路を閉じない。GitHub公式も「attestation自体はビルド処理の正当性を保証しない」ことを明記しており、Option 3を採用しても親 #2093 が求める「偽装producerでもSUCCESSにできない」という性質を完全には満たせない。同時に、caller側（PR headから発火するworkflow）へ`attestations: write`/`id-token: write`を付与する必要があり、これは信頼境界を広げる方向のtrade-offである。
- Option 2（full-SHA pinningのみ）は、workflow定義の改変不可性は得られるが、それ自体はOption 1が既に持つ保証（base-locked evaluator materialization, Issue #2019 P0-6）を大きく超えるものではなく、`ci.yml`からのproducer job切り出しという移行コストに見合う追加保証が小さい。
- Option 1（現状維持）は、既に実装済みの3層防御（cross-run/attempt CheckRun substitution、artifact run/attempt整合性、V3 manifest identity整合性）を後退させず、same-run producer authenticityの未保証範囲を正確に文書化することで、コスト0で現状の透明性を確保する。

以上の費用対効果（移行コスト・権限拡張・保守負担 vs. 個人趣味開発における実害の限定性）から、現時点ではOption 1を維持し、fixed producer + attestation architectureの採用をDEFERする。

## Residual Risk
- **same-run producer spoofingは技術的に防止されていない。** これはrisk acceptanceであり、technical preventionではない。PRを作成できる攻撲者（本プロジェクトでは主にリポジトリ所有者自身、または招待されたcollaborator）が、同一run内で`visual-impact-policy` jobのCheckRun API呼び出しシーケンスを改変し、実際にはVRT比較を実行せずに`conclusion: success`を報告するCheckRunを生成した場合、trusted consumer側の現行検証（exact workflow_run_id/run_attempt binding、V3 manifest identity整合性）はこれを検出できない。
- 親 #2093 のOWNERコメントが要求する「偽装producerでもSUCCESSにできない」という強い保証目標は、本Decisionにより**未達のまま残る**。この残存gapは、DEFER期間中は明示的なrisk acceptanceとして扱う。
- `visual-impact-policy` が現在required checkとしてGitHub側に登録されていないため、現状の実害は限定的（人間の最終レビューが常に介在する）が、将来required check化された場合は本Residual Riskの実効性が変化する（`## Reconsideration Trigger` 参照）。

## Reconsideration Trigger
以下のいずれかが発生した場合、本Decisionを再検討する:

1. `visual-impact-policy` / `visual-impact-policy-trusted` がGitHub branch protection / rulesetのrequired checkとして登録され、かつ信頼できないcollaborator（外部フォークからのPR等）からの直接PRを受け入れる運用に変わった場合
2. 親 #2093 のOWNERが、same-run producer authenticityをmandatory guaranteeとして明示的に再要求した場合
3. GitHub Actions attestationの権限モデル・料金体系・信頼境界仕様が変更され、caller側への権限拡張なしに同等の保証が得られるようになった場合
4. component-vrt-report / visual-impact-policy job の偽装が実際に発生した、または発生しうる具体的なインシデント・攻撃シナリオが確認された場合

## Trust Boundary
N/A — Decision が DEFER のため、trust boundary（signer identity / consumed evidence / execution identity / producer responsibility / guarantee boundary）の確定は不要。Option 3 採用時の設計要件は `## Considered Options（検討した選択肢）` の Option 3 に記載した論点を再検討の出発点とする。

## Migration Plan
N/A — Decision が DEFER のため、段階移行計画は不要。将来 ADOPT へ再決定された場合、`## Reconsideration Trigger` の条件成立を契機に、互換consumer先行 → 参照可能な固定producer → caller切り替え → enforcement、という段階移行と、既存CheckRun照合・job outputs・V3整合性・CI判定器（`.claude/skills/pr-review-judge/scripts/ci_verdict_summary_v2.py` の `("ci", "visual-impact-policy"): "required"` 分類）との互換確認を必須とすることを、follow-up Issueの設計前提として記録する。

## Verification Design
N/A — Decision が DEFER のため、新規検証設計の確定は不要。既存の検証資産（`scripts/agent-ops/tests/test_visual_impact_v3_manifest_seams.py` 等のV3 seam test、`scripts/agent-ops/tests/test_resolve_visual_impact_checkrun_adversarial.py` のCheckRun adversarial test）は、cross-run/attempt substitution・V3 identity整合性の既存保証を検証する現行資産として維持する。将来ADOPT時は、これらの既存資産を拡張し、正常系（legitimate producer/evidence → trusted verifier success）と境界negative control（producer省略・別job artifact混入・signer digest不一致・検証API失敗の区別）を追加する方針を、再決定時の設計前提として記録する。

## ZIP Resource Bound
untrusted ZIP artifactの展開は `scripts/agent-ops/resolve_visual_impact.py` 内ではなく、`.github/workflows/visual-impact-trusted-consumer.yml` のtrusted consumer shell内（`gh_api ".../artifacts/${DECISION_ARTIFACT_ID}/zip" > decision.zip` → `unzip -o -q decision.zip -d decision`）で行われている。現状はentry数・展開前の圧縮/展開後総容量に対するpre-extraction resource boundが存在せず、Python側検証（`resolve_visual_impact.py`）は展開後のJSONサイズ検査のみを行う。

本件はattestation採否の決定とは独立した既存の孤立P1 follow-upである（親 #2093 fact-check時点から継続）。対応方針:

- 対応する価値: あり（zip bomb的な展開コスト増大は、attestation採否に関わらず現存するresource-exhaustion面のリスクであり、小さな改善で閉じられる）
- 対応範囲: 既知のentry（`decision.json` / `evidence.json` 相当ファイル）のみを対象entry限定で取得し、downloaded bytes・entry数・実際に読み出すbytesの小さな上限を設ける、bounded retrievalの改善に限定する。新規の汎用archive防御基盤は不要
- 実装場所: 本Issue（#2101）のAllowed Pathsはこの1 ADRファイルのみであり、`.github/workflows/visual-impact-trusted-consumer.yml` の変更は対象外。follow-up Issueとして起票するかどうかは、本Issue完了の必須条件にしない（follow-up Issue番号の事前確保を要求しない）

## Consequences（帰結）

- 既存の3層防御（cross-run/attempt substitution・artifact run/attempt整合性・V3 manifest identity整合性）は変更されず維持される。
- same-run producer authenticityの未保証範囲が、初めて明示的なADRとして文書化され、親 #2093 との残存gapが可視化される。
- fixed producer / attestation architectureへの実装作業は行われない（Allowed Pathsもこの1ファイルに限定されており、実コード変更はそもそも本Issueの範囲外）。
- ZIP resource boundは、attestation採否と独立の既存follow-up候補として本ADRに記録されたが、follow-up Issueの起票自体は本Issueの完了条件ではない。

## References（参考文献）

- Issue #2101（本ADRの起点）: https://github.com/squne121/loop-protocol/issues/2101
- Issue #2093（親delivery-rollup）: https://github.com/squne121/loop-protocol/issues/2093
- Issue #2091（trusted-side re-derivation実装元）
- PR #2229（CheckRun provenance binding, `verify_component_vrt_checkrun_provenance()`）
- PR #2379 / PR #2388（V3 manifest envelope, run_attempt digest化）
- Issue #2230（artifact-attempt binding, workflow_run_id/run_attempt schema必須化）
- `scripts/agent-ops/resolve_visual_impact.py`（`verify_component_vrt_checkrun_provenance()` docstring、same-run producer authenticityのattestation boundary明記コメント）
- `.github/workflows/ci.yml`（`visual-impact-policy` job定義）
- `.github/workflows/visual-impact-trusted-consumer.yml`（trusted consumer、ZIP展開箇所）
- `docs/dev/visual-impact.schema.json`（`VISUAL_IMPACT_DECISION_V1` schema）
- `docs/ops/branch-protection.md`（required check registration drift記録）
- `.claude/skills/pr-review-judge/scripts/ci_verdict_summary_v2.py`（`CLASSIFICATION_MAP` の `("ci", "visual-impact-policy"): "required"`）
- GitHub公式ドキュメント「Reusing workflow configurations」（reusable workflowのSHA固定・呼び出し元checkout挙動に関する一次資料。Option 2の検討で参照）
- GitHub公式ドキュメント「Artifact attestations concept」および「SLSA v1 Build Level 3」ガイド（attestation predicateの限界・必要な権限要件に関する一次資料。Option 3の検討で参照）
- GitHub CLIの`gh attestation verify`コマンドのマニュアル（`--signer-workflow` / `--signer-digest`オプションの仕様確認元。Option 3の検討で参照）

## Notes

- ファイル名 `0007-visual-impact-immutable-evidence-producer.md` はIssue #2101のAllowed Pathsの指定通りだが、`docs/adr/0007-agent-retrospective-boundaries.md` が既にadr_id "0007"を使用しているため、本ADRのadr_idは次の空き番号 "0009" を採番した。ファイル名prefixとadr_idの不一致の解消（リネーム）は本Issueのスコープ外であり、follow-up Issue候補として記録するに留める。
