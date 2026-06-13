# PR Evidence Checks

## Required PR セクション

- `## 受け入れ条件の達成状況`
- `## 検証コマンド結果`
- `## Allowed Paths 遵守`

## 判定

- AC coverage: 各 AC が `[x]/[ ] + 根拠` で記載
- Allowed Paths: 変更ファイルが issue contract を逸脱しない
- 検証結果: 各 VC が記録されている
-  placeholder（`<達成（根拠）>`）は未達成扱い

## Runtime immediate

- `decision: immediate` 時に `## Runtime Verification Evidence` と artifact/ログの参照が必要
- evidence が一切無い場合は blocker

## Multi-linked issue

複数 linked issue の場合は Issue ごとの AC coverage matrix が必要。
