# docs/product

`docs/product/**` は product SSOT である。

## 最小ルール

- `docs/product/**` と `.specify/**` / Spec Kit 生成物が矛盾する場合、`docs/product/**` を優先する。
- `docs/product/**` は全文再生成しない。既存文書は diff-first で変更する。
- `docs/product/**` の変更は、必ず対応する GitHub Issue / PR を持つ。
- Spec / Plan / Tasks / 実装タスクの作成・レビュー・修正は、既存の `issue-refinement-loop` と `impl-review-loop` の手順に従う。
- Spec Kit の `tasks.md` は tracking SSOT ではない。GitHub Issue 化してから作業する。
