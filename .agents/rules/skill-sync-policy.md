# Rule: skill-sync-policy

skill 改修時の整合性確認手順。

## 1. skill 編集時の preflight

skill `<name>` を編集する前に：

1. `gh grep -r "<name>"` 相当（`grep -r "<name>" .claude/ .agents/`）で参照箇所を列挙
2. 影響を受ける他 skill / SubAgent を特定
3. 変更が破壊的でないか判断（破壊的なら別 PR で分割）

## 2. `required_rules` の整合

- `required_rules:` フロントマターに列挙した rule-id は `.agents/rules/<id>.md` に必ず実体があること
- 存在しない rule-id は CI / pre-commit hook で検出させる（将来）
- rule 追加時は `.agents/rules/index.md` も同 PR で更新

## 3. cross-skill 参照

- skill A が skill B を呼ぶ場合、B の I/O 契約（input / output 構造）を A の本文に明記
- B の I/O 契約が変わったら、参照元 A も同 PR で更新
- 「呼び方が変わったけど呼び元は古いまま」を防ぐ

## 4. SubAgent 参照

- skill が `subagent_type: <name>` を指定する場合、対象 SubAgent が `.claude/agents/<name>.md` に存在すること
- SubAgent 名の改名は cross-cutting なので別 PR

## 5. テンプレート参照

- skill が `.github/ISSUE_TEMPLATE/<file>` を参照する場合、テンプレートが存在すること
- テンプレートの必須項目（YAML の `validations.required`）と skill の preflight チェックが一致していること

## 6. 削除時の整合

- skill を削除する PR では、参照元（他 skill / SubAgent / ドキュメント）の参照も同 PR で削除する
- `.agents/rules/index.md` の該当行も削除

## 関連

- [`skill-rule-boundary`](skill-rule-boundary.md)
