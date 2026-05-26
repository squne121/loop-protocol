# AC / VC Reflection

## Purpose

review 段階で本文品質を評価するとき、baseline 未実装状態と実装後 gate を取り違えないための reference。

## Reflection Rules (SubAgent-owned)

AC/VC の baseline fail 判定、reflection guard、および rewrite 時の期待動作に関する詳細は、`.claude/agents/issue-author.md` の **AC/VC Reflection & Rewrite Logic (SubAgent-owned)** セクションを参照すること。

orchestrator はこれらの判定ロジックを再実装せず、SubAgent 側の自律的判断に委譲する。

## Rewrite guard (orchestrator layer)

- `reviewer_feedback_text` は opaque forwarding payload として扱う。
- anchor comment が絡む場合も、raw snapshot ではなく正規化済み `anchor_comment_feedback` だけを `issue-author` へ渡す。

## Must not

- verification owner が異なる VC を refinement loop 側で再分類しない
- SubAgent 側の reflection 判定を orchestrator 側で再解釈しない
