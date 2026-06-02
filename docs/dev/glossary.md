---
title: 用語集（VC 標準用語）
status: stable
related_issue: "#578"
---

# 用語集

LOOP_PROTOCOL プロジェクトで使用する VC（Verification Commands）周辺用語の定義。

## VC 標準用語

### 自明PASS

- **英語表記**: `trivially-pass`（人間向け説明文での表記）
- **定義**: 検証対象の実装が存在しない、または常に成功する状態であるために VC が PASS してしまうこと。真に実装を検証していない「見かけ上の PASS」を指す。
- **用途**: Issue contract レビュー、VC preflight 判定で使用する。
- **注意**: `trivially_pass` は machine-readable token（enum / YAML key / script / grep pattern）として使用し、名称変更は禁止。人間向け説明文では「自明PASS」を使う。

### 変更前状態

- **英語表記**: baseline（VC 文脈の名詞として使用）
- **定義**: 実装変更を加える前の状態、または比較基準となる初期状態。VC 文脈では「実装前のコード状態」を意味する。
- **用途**: VC 設計の文脈で「変更前の状態で VC がどう動くか」を説明する際に使用する。
- **注意**: standalone `baseline` の prh 自動置換は対象外（文脈依存が強く、誤置換リスクがある）。詳細は後述「standalone baseline の自動置換について」を参照。

### 変更前FAIL

- **英語表記**: `baseline fail`（VC 文脈での表記）
- **定義**: 変更前状態（実装前）では VC が失敗すること。TDD における「先にテストを書き、実装前は Red になる」状態に相当する。正しい VC 設計では変更前FAIL が期待される。
- **用途**: VC 設計レビューで「実装前に VC が失敗するか」を確認する際に使用する。

### 変更前PASS

- **英語表記**: `baseline PASS` / `baseline pass`（VC 文脈での表記）
- **定義**: 変更前状態（実装前）であるにもかかわらず VC が PASS してしまうこと。実装がなくても通る VC は自明PASS（`trivially-pass`）の疑いがある。
- **用途**: VC レビューで「変更前状態でも PASS している場合は VC の有効性を疑うべき」という判断基準として使用する。

## standalone baseline の自動置換について

`baseline` という単語が単独で使用される場合、prh による自動置換は**行わない**。理由は以下の通り：

1. **文脈依存が強い**: `baseline` は「基準値」「初期状態」「比較元」「変更前状態」など複数の意味を持ち、機械的な置換では文脈を誤解する可能性がある。
2. **誤置換リスク**: コードコメント、YAML key、grep pattern に `baseline` を含む箇所があり、一括置換で意図しない変更が生じる恐れがある。
3. **段階的対応**: 完全な用語統一は別 Issue で段階的に対応する。

`baseline fail` / `baseline pass`（複合語）については prh ルールで `変更前FAIL` / `変更前PASS` へ変換する。
