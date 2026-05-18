# Step 3x ドメイン固有検証ステップ 追加テンプレート

本テンプレートは、特定のディレクトリ配下の変更（`{TARGET_DIRECTORY}`）に対して、
特定のキーワード（`{VERIFICATION_KEYWORD}`）に基づく実機検証 evidence の存在と結果を
自動チェックするための `Step 3x` を追加するためのものです。

既存の `Step 3a`（Kindle for PC2 実機検証）を変更してはならない。
新ドメインごとに `Step 3b`, `Step 3c`, ... と連番を振る。

## プレースホルダー一覧

| プレースホルダー | 説明 | Step 3a の実例 |
|---|---|---|
| `{STEP_NUMBER}` | ステップ番号（例: `3b`, `3c`） | `3a` |
| `{DOMAIN_NAME}` | ドメイン識別子（例: `android-app`） | `kindle-ingestion` |
| `{TARGET_DIRECTORY}` | diff 検出対象ディレクトリ（例: `android-app/`） | `src/ingestion/` |
| `{SPEC_TASKS_MD_PATH}` | 対象 tasks.md の相対パス | `.kiro/specs/kindle-content-ingestion/tasks.md` |
| `{VERIFICATION_KEYWORD}` | tasks.md 内の検索キーワード（例: `Android 実機検証`） | `Kindle for PC2 実機検証` |
| `{REPORT_DIRECTORY}` | 検証レポート格納ディレクトリ（例: `android-app/reports/live-verification/`） | `reports/live-verification/` |
| `{PR_NUMBER}` | 適用実績 PR 番号 | `393` |
| `{ISSUE_NUMBER}` | 適用実績 Issue 番号 | `390` |

---

## (1) SKILL.md 挿入用 Markdown スニペット

`SKILL.md` の `Procedure` セクション、既存の Step 3a の次（または適切な位置）に挿入する。

```markdown
{STEP_NUMBER}. **{DOMAIN_NAME} 検証 evidence をチェックする**（`{VERIFICATION_KEYWORD}` 要件がある PR のみ）:
   1. 対象 tasks.md を特定する:
      - `gh pr diff <PR番号> --name-only` の実 diff を正とし、`{TARGET_DIRECTORY}` を含むパスが存在する場合は `{SPEC_TASKS_MD_PATH}` を対象とする。
        ```bash
        gh pr diff <PR番号> --name-only | grep "{TARGET_DIRECTORY}"
        ```
      - PR 本文の `Changed Paths` セクションは参考情報として補助的に参照するが、記載漏れ・記載ミスがあり得るため正とはしない。
      - どちらにも `{TARGET_DIRECTORY}` が含まれない場合はこのステップをスキップする。
   2. tasks.md に `{VERIFICATION_KEYWORD}` が記載されているか確認する:
      ```bash
      rg -n "{VERIFICATION_KEYWORD}" {SPEC_TASKS_MD_PATH}
      ```
      - 1件もマッチしなければこのステップをスキップする。
   3. linked issue 本文または PR 本文から対象 phase/task を特定する（例: `Task 4.2`、`Phase 3` など）。
   4. 対応する検証レポートが `{REPORT_DIRECTORY}` に存在するか確認する:
      ```bash
      ls -1 {REPORT_DIRECTORY} 2>/dev/null || echo "MISSING: reports directory not found"
      ```
      - ファイル命名規則: `phase<N>_<subtask>_<datetime_yyyymmddThhmmss>.json`（例: `phase4_2_20260405T153000.json`）
      - 対象 phase が特定できた場合は phase 番号でフィルタして確認する（例: `ls -1 {REPORT_DIRECTORY}phase4_*.json`）。
   5. 存在する場合、各レポートの `result` フィールドを確認する:
      ```bash
      python3 -c "
      import json, glob
      reports = glob.glob('{REPORT_DIRECTORY}phase*.json')
      for r in sorted(reports):
          d = json.load(open(r, encoding='utf-8'))
          print(f'{r}: result={d.get(\"result\", \"MISSING\")}')
      "
      ```
   6. 以下のいずれかの場合は **blocker** として追加する:
      - 対応する phase/task のレポートが存在しない
      - レポートの `result` フィールドが `pass` でない（`fail` または `partial`）

      blocker テキスト例（`[{DOMAIN_NAME}-verification]` プレフィックスを使う。
      Step 3a の `[live-verification]` は固有名のため、新ドメインでは `{DOMAIN_NAME}` に置き換えて識別性を保つ）:
      ```
      [{DOMAIN_NAME}-verification] tasks.md に `{VERIFICATION_KEYWORD}` が記載されていますが、
      対応する検証レポートが見つかりません（または result が pass ではありません）。
      `{REPORT_DIRECTORY}` に `phase<N>_<subtask>_<datetime>.json` を追加し、
      `"result": "pass"` を確認してから再提出してください。
      ```
```

---

## (2) SKILL.md の Stop Conditions への追記

`SKILL.md` の `## Stop Conditions` セクション末尾に以下を追記する。

```markdown
- tasks.md に `{VERIFICATION_KEYWORD}` が記載されているのに対応する検証レポートが存在しない → `REQUEST_CHANGES`
- `{DOMAIN_NAME}` 検証レポートの `result` フィールドが `pass` でない（`fail` または `partial`） → `REQUEST_CHANGES`
```

---

## (3) best-practices.md の出典記録 更新例

`best-practices.md` の `Step 3x` パターン記述にある「出典」リストに、新ドメインの適用実績を追記する。

```markdown
  - 出典: PR #{PR_NUMBER}（Issue #{ISSUE_NUMBER}）で `{DOMAIN_NAME}` の `{VERIFICATION_KEYWORD}` evidence チェックとして確立。
```

---

## 適用後チェックリスト

新ドメインの Step 3x を追加した後、以下をすべて確認する:

- [ ] `bash scripts/sync-agent-skills.sh --check` で drift なし（`.agents/skills/` → `.claude/skills/` 同期確認）
- [ ] `SKILL.md` の `## Related` セクションに新規 reference を追記した（必要な場合）
- [ ] `SKILL.md` の `## Stop Conditions` セクションに新ドメイン向け条件 2 行を追記した
- [ ] `best-practices.md` の Step 3x 出典リストに適用実績（PR番号・Issue番号）を追記した
- [ ] `just check` / CI が全 pass であることを確認した

---

## 出典

- 確立: PR #393（Issue #390）で `Kindle for PC2 実機検証` 要件の live 検証 evidence チェック（Step 3a）として確立。
- テンプレート化: Issue #395 にて Step 3a の構造を汎化。
