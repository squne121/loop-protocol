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

## AI運用語の扱い

AI 運用語は、複合語の blanket replacement ではなく atomic term 単位で扱う。`CLAUDE.md` / `AGENTS.md` / agent prompt に長い用語表を増やさず、repo 側の glossary / prh で post-processing する。

| 英語表記 | 日本語表示 | 分類 | 定義 | 自動修正方針 |
| --- | --- | --- | --- | --- |
| `auto-fixable` | 自動修正可能 | 安全自動修正 | プロセス・フック・VC の正確性を損なわない自動修正対象 | 複合語のみ置換 |
| `hygiene failure` | 体裁不備 | 安全自動修正 | コード体裁・書式の不統一 | 複合語のみ置換 |
| `intake gate` | 着手前確認 | 文脈限定自動修正 | 実装着手前に確認・判断が必要な gate / decision point | 複合語のみ置換、`intake_gate` は不変、`` `intake_gate` `` は不変 |
| `deterministic fixer SubAgent` | 決定的修正サブエージェント | 文脈限定自動修正 | 確定的・決定的な修正を行う SubAgent（複数経路がなく、結果が一意に定まるもの） | 複合語のみ置換、snake_case 不変、code span 不変 |
| `snapshot freshness` | スナップショット鮮度 | 文脈限定自動修正 | snapshot / contract / state の取得時点が現在の状態とどれだけ齟齬がないか | 複合語のみ置換、snake_case 不変、code span 不変 |
| `enum` | enum | 用語集のみ | schema / code token としてそのまま維持する | 単独自動修正しない |
| `mutation` | mutation | 用語集のみ | code / contract token としてそのまま維持する | 単独自動修正しない |
| `stale` | stale | 用語集のみ | 文脈依存が強いため単独自動置換しない | 単独自動修正しない |
| `intake` | intake | 用語集のみ | 単独では自動置換しない（複合語 `intake gate` で使用） | 単独自動修正しない |
| `gate` | gate | 用語集のみ | 単独では自動置換しない（複合語 `intake gate` で使用） | 単独自動修正しない |
| `freshness` | freshness | 用語集のみ | 単独では自動置換しない（複合語 `snapshot freshness` で使用） | 単独自動修正しない |
| `snapshot` | snapshot | 用語集のみ | 単独では自動置換しない（複合語 `snapshot freshness` で使用） | 単独自動修正しない |
| `SubAgent` | SubAgent | 用語集のみ | 単独では自動置換しない（複合語で使用） | 単独自動修正しない |

`prose` / `heading` / `handoff` / `enforcement` も atomic term として文脈依存が強いので、単独の blanket replacement では扱わず、必要なら別の contextual rule に分離する。

英語ラベルの残存確認は **リポジトリ全体**（`docs/` / `.github/` / `CLAUDE.md` / `AGENTS.md` / `README.md`）を対象に行う。prh ルールには、複合語のみ置換し snake_case トークン・`` `code span` `` を不変に保つ **境界を壊さない検証例** を含める。**スキーマ項目名**（`enum` 値 / `YAML key` / `schema property`）は人間向け表示の降格対象に含めず、英語表記のまま維持する。

GitHub に投稿済みの Issue / PR コメントは遡及修正しない。人間向け prose の整形は、投稿前 Markdown または repo 内文書に限定する。
