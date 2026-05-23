# docs/product

## 位置づけ

`docs/product/` は **プロダクト SSOT（単一の真実の情報源）**。
product spec、ゲーム設計、MVP スコープ、仕様ライフサイクルに関する正本はここに置く。

## docs/product/** を操作するときのルール

`docs/product/**` を作成・更新・archive・tasks.md 変換する場合は、以下の入口を使う:

1. **path-scoped rule**: `.claude/rules/product-spec-lifecycle.md`（`paths: ["docs/product/**"]` スコープ）
2. **手順 skill**: `.claude/skills/product-spec-lifecycle/SKILL.md`
3. **ライフサイクル正本**: `docs/dev/product-spec-lifecycle.md`（状態遷移・token_policy・EARS 等の詳細）

## full_regeneration 禁止

```yaml
full_regeneration: prohibited
```

spec を全文再生成しない。必ず diff-first（差分更新）で操作する。
再生成された出力は既存 GitHub Issues と乖離することが多く、SSOT を破壊する。

## Spec Kit / `.specify/` 由来 artifact との関係

`.specify/` 配下の artifact は **derived workbench**（派生作業場）であり、SSOT ではない。
`docs/product/**` の内容と `.specify/` 由来 artifact が矛盾する場合は **`docs/product/**` が勝つ**。

詳細は `docs/adr/0002-sdd-tool-adoption.md` §"generated_artifact_boundary" を参照する。
