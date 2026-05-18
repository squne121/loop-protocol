# Rule: issueops-mode-guard

Issue 種別（mode）の判定とラベル / テンプレート整合のガード。

## 1. mode の定義

| mode | title prefix | template | 代表ラベル | 着手者 |
|---|---|---|---|---|
| research | `調査:` / `research:` | `.github/ISSUE_TEMPLATE/research.yml` | `phase/research` | AI（読み取りのみ）/ 人間 |
| implementation | `実装:` / `implement:` | `.github/ISSUE_TEMPLATE/implementation.yml` | `phase/implementation` | AI（実装可） |
| human-confirm | `人間判断:` / `human-confirm:` | `.github/ISSUE_TEMPLATE/human-confirm.yml` | `state/needs-human` | 人間 |

## 2. mode 判定ロジック

skill / SubAgent は以下の順で判定する：

1. title prefix を確認
2. ラベルを確認
3. 本文内のテンプレートセクション（見出し）を確認

3 つが一致していなければ「mode 不整合」として人間判断を仰ぐ（`state/needs-human` 付与）。

## 3. mode 別の振る舞い

- **research**: AI は `src/`、`tests/` を変更しない。調査結果は Issue コメント or 別の研究用ドキュメントへ
- **implementation**: AI は受け入れ条件と Allowed Paths の範囲内で実装。PR まで進める
- **human-confirm**: AI は着手しない。Issue 本文の論点を整理する補助のみ

## 4. mode 切替

- research → implementation の昇格時は、別 Issue にする（同 Issue で mode を切り替えない）
- implementation の途中で「実は research が必要」と判明したら、現 Issue を一旦保留して research Issue を新規起票

## 関連

- [`issue-uncertainty-policy`](issue-uncertainty-policy.md)
- [`issue-body-ssot-policy`](issue-body-ssot-policy.md)
- [`issueops-common-guard`](issueops-common-guard.md)
