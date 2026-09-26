# PR Evidence Checks

## Required PR セクション

- `## 受け入れ条件の達成状況`
- `## 検証コマンド結果`
- `## Allowed Paths 遵守`

## 判定

- AC coverage: 各 AC が `[x]/[ ] + 根拠` で記載
- Allowed Paths: 変更ファイルが issue contract を逸脱しない
- 検証結果: 各 VC が記録されている
-  placeholder（`<達成（根拠）>`）は未達成扱い

## Runtime immediate

- `decision: immediate` 時に `## Runtime Verification Evidence` と artifact/ログの参照が必要
- evidence が一切無い場合は blocker

## Multi-linked issue

複数 linked issue の場合は Issue ごとの AC coverage matrix が必要。

## Enumerated / Exhaustive Claim Evidence Coverage（有限の列挙集合・全称 Claim の証拠基準、Issue #2765）

AC または Safety Claim が有限の列挙集合について all / each / 全対応 / 非回帰などの exhaustive completeness を主張する場合（finite/exhaustive claim）にのみ、本節の rule を適用する。単なる例示列挙（「例: A/B」「代表例として A/B/C」「A、B など」）は対象外であり、この rule で blocker 化しない。

対象と判定した場合、aggregate な suite/class の PASS 件数（例: `pytest -q -> 6 passed` のようなクラス単位の合計）のみを、列挙された全ケースの直接 coverage evidence として受理しない。各列挙ケースについて次の両方（AND）を確認し、いずれか一方でも欠落するケースが1つでもあれば、current-head CI が green でも `REQUEST_CHANGES` blocker とする。

1. **case identity**: named node / parameter ID / table-driven input / test source のいずれかで、そのケースが実際に対象になっていることを確認する。named node や parameter ID が存在するだけでは coverage の証明にならない（parameterization は各 parameter set を個別 invocation として収集するが、ID の存在自体は該当ケースが Claim の要求する behavior を assert したことを意味しない）。
2. **relevant executable assertion が PASS**: そのケースが Claim の要求する observable behavior を実際に assert し、PASS していることを確認する。skip / xfail / 未実行のケースは、AC または Claim が明示的に許可しない限り direct evidence として扱わない（pytest はケース単位で `xfail` を付けても suite 全体を正常終了させられるため、aggregate PASS 件数だけでは skip/xfail 混入を検出できない）。

新しい deterministic validator / schema / persistent database / heavyweight analyzer（test-ownership database、常時 AST 解析、新しい LLM judge 等）は追加しない。本節は既存 reviewer の semantic review criteria への判定規則の具体化に留まる。

本節は Issue #2757（Issue authoring 時の existing-test discovery / CI coverage preflight）とは責務が異なる。#2757 は Issue 起票時点の事前確認であり、本節は実装後 PR review 時点での Claim ↔ evidence の semantic coverage 品質を扱う。

### Historical regression example（PR #2763、再発防止のみが目的）

PR #2763 の source-review head `ce73605b6a442f6b88eafe30a3feda4bf62ac3dc`（review comment https://github.com/squne121/loop-protocol/pull/2763#issuecomment-5832848503 が対象にした時点）では、`TestRedaction -q -> 6 passed` という aggregate PASS が、Issue #2728 AC3 の4トークン形式（classic `ghp_` / classic `ghs_` / `github_pat_` / bare JWT）のうち classic `ghs_` / `github_pat_` / bare JWT の直接証拠として誤って採用され得た（classic `ghp_` 以外は executable assertion で被覆されていなかった）。その後 PR #2763 は commit `9d6d6b7a932b752059a016f0a301f2ccfcd72336`（`ids=["classic_ghp","classic_ghs","github_pat","bare_jwt"]` の4-case parametrize を追加）で修正され、最終 merge HEAD `c7dc65a7510afb172dc6fab19cc914c279cf25c7` で解消済みである。**現在の PR #2763 はこの failure の reproducer ではない**。本節が防止するのは、この pre-fix HEAD で発生したのと同種の再発である。
