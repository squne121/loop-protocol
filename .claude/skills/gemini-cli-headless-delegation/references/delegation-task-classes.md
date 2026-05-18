# Delegation Task Classes

Gemini に渡す仕事の責務境界を固定する 4 区分。実装難易度ではなく責務境界で分類する。
（出典: #124, [#124 コメント](https://github.com/squne121/KindleAudiobookMakeSystem/issues/124#issuecomment-4186909088)）

## 定義

| Class | 説明 | Gemini 委譲 | 期待成果物 |
|-------|------|-------------|-----------|
| R0 | read-only evidence extraction（ログ要約・根拠行列挙・比較表） | 可 | `delegation_result_v1` 本文 + evidence section |
| R1 | bounded draft generation（boilerplate・テンプレ・issue/comment 草案） | 可（草案まで） | draft のみ。apply は caller / 人間 |
| R2 | deterministic refactor assist（rename 案・import 整理・patch draft） | 変更案まで | 実 apply と validation は Claude/Codex 側 |
| R3 | behavior-changing / design-sensitive work（アーキ設計・状態遷移・セキュリティ境界） | 不可 | Claude/Codex / 主担当で処理 |

### R1 の追加条件
- 対象 path / file count / expected shape を request に明示すること。

### R2 の追加条件
- Gemini は「変更案の列挙」または「patch draft」まで担当。
- 実 apply と validation は必ず Claude/Codex 側に残す。

## 評価マトリクス

`[wrapper | contract | operations | evaluation]` × `[deterministic-ish | grounded/live]` で評価する。

| 評価軸 | deterministic-ish | grounded/live |
|--------|------------------|---------------|
| wrapper | 固定 context、no_tools、shape-based validation | grounded_research、実モデル依存 |
| contract | request/result schema の再現性 | live 品質（stats.models 有無含む） |
| operations | retry/timeout/fail-closed の再現性 | 実ネットワーク・429 対応の品質 |
| evaluation | golden task 再現性、task class 別合否判定の安定性 | live 品質スコア、根拠精度の観察 |

## Task Class 別 Validation 観点

| Class | Validation 観点 |
|-------|----------------|
| R0 | 根拠抽出の再現性、context 外参照の抑止、evidence 欠落率 |
| R1 | 雛形の shape 一貫性、既存 pattern 追従性、無関係な創作混入率 |
| R2 | patch draft の boundedness、影響範囲の列挙、validation command 提示率 |
| 共通 | fail-closed、stderr 保持、missing context file、timeout、429、JSON parse failure |

### `local_asset_research` の R0 境界

`local_asset_research` は R0 のうち「repo 内のローカル資産調査」に限定する。Gemini CLI は Serena MCP の read-only tool（`find_file` / `find_referencing_symbols` / `find_symbol` / `get_symbols_overview` / `list_dir` / `search_for_pattern`）だけを使い、根拠抽出・構造把握・候補パス特定までを担当する。

次の作業は R0 を超えるため禁止する:

- shell execution
- file edit / shell write
- GitHub write
- repo 外の任意読み取り
- `post_to_issue_url` による自動投稿
- Serena MCP の危険 tool または未検証 MCP tool を使う調査

上記が必要な場合、wrapper は `grounded_research` へ曖昧に流用せず fail-closed する。

## fail-closed の定義

fail-closed とは「実行しない」だけでなく、**「適切な failure 情報を返す」** ことを含む。

具体的には以下の状態を指す:
- `delegation_result_v1` の `ok == false`
- `warnings` / `stderr` に failure reason が入っている

後続の自動判定が可能な機械可読 failure 情報を返すことが fail-closed の要件。

## `--approval-mode plan` の制約

wrapper は Gemini CLI を `--approval-mode plan` で実行する（`SKILL.md` Core Rules 参照）。

**制約**:
- read-only 前提。file edit / shell execution は禁止。
- `tool_profile=grounded_research` の場合も Google Search のみ許可。
- `tool_profile=local_asset_research` の場合も Serena MCP read-only local asset research のみ許可し、危険 tool または未検証 MCP 設定があれば実行しない。
- この制約により、出力品質（特に R1/R2 の draft 生成）は入力 context の質に依存する。
  context_files の選定と `inline_context` の補足が品質向上の主要手段。

## Golden Tasks（参考例）

- build log から失敗要約 + 根拠 5 件（R0）
- 既存 pytest pattern に合わせた test skeleton 草案（R1）
- 指定ディレクトリ 3 ファイル以内の rename / import fix 案（R2）
- issue コメント草案（事実 / 推論 / 提案の分離あり）（R1）
