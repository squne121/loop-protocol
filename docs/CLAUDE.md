# docs

## 位置づけ

`docs/` 配下は **プロジェクトの単一の真実の情報源（SSOT）**。仕様・運用ルール・アーキテクチャ決定の正本。
タスク着手前の SSOT 探索は `.claude/skills/ssot-discovery` を経由する（再現可能性のため）。

## 編集規約

- 大きな運用変更は ADR として `docs/adr/NNNN-<topic>.md` を追加
- 既存 SSOT を変更する PR は、影響を受ける skill / agent / コードを同 PR で更新する
