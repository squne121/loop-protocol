# AC / VC Reflection

## Purpose

review 段階で本文品質を評価するとき、baseline 未実装状態と実装後 gate を取り違えないための reference。

## Rules

- refinement フェーズでは AC を実行しない
- Verification Commands の baseline 0 hit や file-not-found は、implementation 前なら expected baseline fail として扱う
- 「現行コードに変更がない」は blocker ではない
- Stop Conditions に書かれた実装中の検出条件を、refinement 時点で満たす必要はない
- issue-author は review-issue の opaque feedback を受け取って rewrite し、reviewer の domain judgment を main thread が再解釈しない

## Rewrite guard

- `reviewer_feedback_text` は opaque forwarding payload として扱う
- anchor comment が絡む場合も、raw snapshot ではなく正規化済み `anchor_comment_feedback` だけを追加する
- baseline fail expectation を消すために AC/VC を弱めない

## Must not

- baseline fail を「本文が壊れている」の証拠として扱わない
- verification owner が異なる VC を refinement loop 側で再分類しない
