# Rule: issue-uncertainty-policy

Issue に残る不確実性の扱い。ラベルで明示し、勝手に推測で埋めない。

## 1. 不確実性の分類

| 種類 | ラベル | 説明 |
|---|---|---|
| 仕様未確定 | `state/needs-human` | 人間判断が必要。AI は着手しない |
| 調査未完了 | `phase/research` + `state/queued` | 調査タスクとして扱う。実装に進まない |
| 実装可能 | `phase/implementation` + `state/queued` | 契約が固まっており実装着手可 |
| 実装中 | `phase/implementation` + `state/in-progress` | 着手済み |
| 完了 | `state/done` | クローズ条件を満たす |

## 2. 推測禁止

- 受け入れ条件に「曖昧な記述」「複数の解釈が可能な表現」がある場合、勝手に解釈して進めない
- `## Notes for Reviewer` セクションに不確実点を残すか、`state/needs-human` ラベルで人間判断を仰ぐ
- Issue contract が確定するまでブランチ / PR を作らない

## 3. タイトル prefix の自己チェック

- `調査:` / `research:` で始まる Issue は AC に実装変更（`src/`、`tests/` 等）を含めない
- 含まれている場合は `実装:` / `implement:` に切り替えるか、Scope を分割して別 Issue 化

## 4. ready tuple

実装着手可能な canonical な状態：
- title prefix: `実装:` または `implement:`
- labels: `phase/implementation` + `state/queued`
- Allowed Paths / 受け入れ条件 / Verification Commands が本文に明記

## 関連

- [`issueops-mode-guard`](issueops-mode-guard.md)
- [`issue-body-ssot-policy`](issue-body-ssot-policy.md)
